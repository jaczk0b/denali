"""Kolejka „do zatwierdzenia”: obiekty zgłoszone przez integracje czekają, aż jednym kliknięciem staną się urządzeniami.

Integracja opisuje obiekt słownikiem `proposed`:
  name, kind, app_url (opcjonalnie)
  addresses   [{"kind": ip|mac|hostname, "value": …}]
  parent      na czym działa: {"ref": external_id tej samej integracji} albo {"ip"|"mac"|"hostname": …}
  depends_on  [{"ref"|"ip"|"mac"|"hostname": …, "type": dane|siec|zasilanie|usluga, "hard": bool}]
  uplink      przez co jest teraz w sieci: {"ref"|…: …, "hard": bool} — zmienia się (telefon przechodzi między AP),
              więc nie zapisujemy go przy zatwierdzeniu, tylko odświeżamy przy każdym odczycie (integrations.py)
  live_deps   [{"ref"|"ip"|"mac"|"hostname": …, "type": …, "hard": bool}] — jak uplink, dowolnego rodzaju
              (np. montaż NFS → udział na TrueNAS, z agenta); też odświeżane przy każdym odczycie

Adres pozwala połączyć obiekty z różnych integracji: stack z Komodo trafia na VM z Proxmoxa o tym samym IP.
Zatwierdzenie jednego obiektu zatwierdza też czekające obiekty, na których działa, i te, które działają na nim —
nowa VM ze stackami to jedno kliknięcie.

Usunięte w źródle (VM, LXC, stack — przedrostki `Adapter.removable`): gdy udane odczyty nie zgłaszają obiektu przez
GONE_AFTER, czekający i pominięci znikają z kolejki sami, a zatwierdzony trafia do kolejki z pytaniem: usunąć
urządzenie z Denali czy zostawić je w dokumentacji bez powiązania. Sami niczego nie kasujemy — brak obiektu może
też znaczyć zmienione uprawnienia tokenu.
"""
import time

from sqlalchemy import delete, select

from . import auth, inventory
from .models import device, external_ref, integration, observed_state

GONE_AFTER = 600   # s bez obiektu w udanych odczytach — kilka odczytów, nie jedna dziura

TARGET_KEYS = ("ref",) + inventory.ADDRESS_KINDS


def _sanitize(external_id, proposed):
    p = dict(proposed or {})
    p["name"] = str(p.get("name") or external_id).strip()[:200]
    if p.get("kind") not in inventory.KINDS:
        p["kind"] = "inne"
    addrs = []
    for a in p.get("addresses") or []:
        try:
            addrs.append({"kind": a["kind"], "value": inventory.normalize_address(a["kind"], a["value"])})
        except (inventory.InventoryError, KeyError, TypeError):
            continue
    p["addresses"] = addrs
    return p


def upsert(conn, integration_id, external_id, proposed, now=None):
    """Integracja zgłasza obiekt. Zwraca (id, czy_nowy). Zatwierdzonych i odrzuconych nie rusza poza `proposed`."""
    now = now or time.time()
    proposed = _sanitize(external_id, proposed)
    row = conn.execute(select(external_ref).where(external_ref.c.integration_id == integration_id,
                                                  external_ref.c.external_id == external_id)).first()
    if row:
        conn.execute(external_ref.update().where(external_ref.c.id == row.id).values(proposed=proposed, last_seen=now))
        return row.id, False
    rid = conn.execute(external_ref.insert().values(integration_id=integration_id, external_id=external_id,
                                                    proposed=proposed, first_seen=now, last_seen=now)
                       ).inserted_primary_key[0]
    return rid, True


def pending(conn):
    return conn.execute(select(external_ref).where(external_ref.c.status == "nowy").order_by(external_ref.c.id)
                        ).mappings().all()


def _get(conn, ref_id):
    r = conn.execute(select(external_ref).where(external_ref.c.id == ref_id)).mappings().first()
    if not r:
        raise inventory.InventoryError("Nie ma takiego obiektu w kolejce.")
    return r


def _key(target):
    for k in TARGET_KEYS:
        if (target or {}).get(k):
            return k, target[k]
    return None, None


def _target_device(conn, integration_id, target):
    k, v = _key(target)
    if k == "ref":
        r = conn.execute(select(external_ref.c.device_id).where(
            external_ref.c.integration_id == integration_id, external_ref.c.external_id == v,
            external_ref.c.status == "zatwierdzony")).first()
        return r.device_id if r else None
    return inventory.find_by_address(conn, k, v) if k else None


def _points_to(ref, target, from_integration):
    """Czy cel `target` (zgłoszony przez integrację from_integration) wskazuje na czekający obiekt `ref`."""
    k, v = _key(target)
    if k == "ref":
        return ref["integration_id"] == from_integration and ref["external_id"] == v
    if k:
        try:
            v = inventory.normalize_address(k, v)
        except inventory.InventoryError:
            return False
        return any(a["kind"] == k and a["value"] == v for a in ref["proposed"]["addresses"])
    return False


def _closure(conn, start):
    """Czekające obiekty do zatwierdzenia razem ze `start`: przodkowie (na czym działa) i potomkowie.
    Rodzeństwo (inne VM na tym samym hoście) czeka dalej."""
    waiting = [r for r in pending(conn) if r["id"] != start["id"]]
    chosen = {start["id"]: start}
    for direction in ("up", "down"):
        todo = [start]
        while todo:
            cur = todo.pop()
            for r in waiting:
                if r["id"] in chosen:
                    continue
                if direction == "up":
                    hit = _points_to(r, cur["proposed"].get("parent"), cur["integration_id"])
                else:
                    hit = _points_to(cur, r["proposed"].get("parent"), r["integration_id"])
                if hit:
                    chosen[r["id"]] = r
                    todo.append(r)
    return chosen


def _create(conn, ref, now):
    p = ref["proposed"]
    fields = {}
    if str(p.get("app_url") or "").startswith(("http://", "https://")):
        fields["app_url"] = p["app_url"]
    dev_id = inventory.create_device(conn, p["name"], p["kind"], now=now, **fields)
    for i, a in enumerate(p["addresses"]):
        inventory.add_address(conn, dev_id, a["kind"], a["value"], source="integracja", primary=(i == 0))
    parent = _target_device(conn, ref["integration_id"], p.get("parent"))
    if parent:
        try:
            inventory.update_device(conn, dev_id, parent_id=parent, now=now)
        except inventory.InventoryError:
            # typ nie pozwala „działać na” rodzicu — nie gubimy informacji, zapisujemy jako twardą zależność
            inventory.add_dependency(conn, dev_id, parent, "usluga", hard=True, source="integracja")
    for dep in p.get("depends_on") or []:
        target = _target_device(conn, ref["integration_id"], dep)
        if target and dep.get("type") in inventory.DEP_TYPES:
            try:
                inventory.add_dependency(conn, dev_id, target, dep["type"], hard=bool(dep.get("hard", True)),
                                         source="integracja")
            except inventory.InventoryError:
                pass   # pętla z danych integracji — pomijamy tę jedną zależność
    conn.execute(external_ref.update().where(external_ref.c.id == ref["id"]).values(status="zatwierdzony",
                                                                                  device_id=dev_id))
    return dev_id


def approve(conn, ref_id, user="system", ip="", now=None):
    """Zatwierdza obiekt (i powiązane czekające). Zwraca listę nowych urządzeń w kolejności tworzenia."""
    now = now or time.time()
    start = _get(conn, ref_id)
    if start["status"] != "nowy":
        return []
    left = dict(_closure(conn, start))
    created = []
    while left:
        # najpierw te, których rodzic nie czeka w tej samej partii — wtedy rodzic już istnieje jako urządzenie
        ready = [r for r in left.values()
                 if not any(_points_to(o, r["proposed"].get("parent"), r["integration_id"])
                            for o in left.values() if o["id"] != r["id"])] or list(left.values())[:1]
        for r in sorted(ready, key=lambda r: r["id"]):
            created.append(_create(conn, r, now))
            auth.audit(conn, "device_approve", user=user, ip=ip, target=r["proposed"]["name"],
                       params={"external_id": r["external_id"], "integration_id": r["integration_id"]})
            del left[r["id"]]
    return created


def approve_all(conn, user="system", ip="", now=None):
    created = []
    for r in pending(conn):
        if _get(conn, r["id"])["status"] == "nowy":
            created += approve(conn, r["id"], user=user, ip=ip, now=now)
    return created


def link(conn, ref_id, device_id, user="system", ip=""):
    """Obiekt z integracji to urządzenie, które już jest (np. przeniesione z HLC) — przypinamy zamiast tworzyć nowe."""
    ref = _get(conn, ref_id)
    dev = inventory.get_device(conn, device_id)
    if not dev:
        raise inventory.InventoryError("Nie ma takiego urządzenia.")
    for a in ref["proposed"]["addresses"]:
        inventory.add_address(conn, device_id, a["kind"], a["value"], source="integracja")
    conn.execute(external_ref.update().where(external_ref.c.id == ref_id).values(status="zatwierdzony",
                                                                               device_id=device_id))
    auth.audit(conn, "device_link", user=user, ip=ip, target=dev["name"], params={"external_id": ref["external_id"]})


def suggest_matches(conn):
    """{ref_id: urządzenie} — czekające obiekty, które wyglądają na urządzenia już obecne w Denali
    (np. przeniesione z HLC): ten sam MAC, IP albo nazwa. Tylko jednoznaczne dopasowania."""
    linked = {(r.integration_id, r.device_id) for r in conn.execute(
        select(external_ref.c.integration_id, external_ref.c.device_id).where(external_ref.c.status == "zatwierdzony"))}
    devices = conn.execute(select(inventory.device.c.id, inventory.device.c.name, inventory.device.c.kind)).mappings().all()
    by_name = {}
    for d in devices:
        by_name.setdefault(d["name"].strip().lower(), []).append(d)
    out = {}
    for ref in pending(conn):
        candidates = []
        for kind in ("mac", "ip"):
            for a in ref["proposed"]["addresses"]:
                if a["kind"] == kind:
                    did = inventory.find_by_address(conn, kind, a["value"])
                    if did:
                        candidates.append(did)
            if candidates:
                break
        if not candidates:
            candidates = [d["id"] for d in by_name.get(ref["proposed"]["name"].strip().lower(), [])]
        candidates = [c for c in dict.fromkeys(candidates) if (ref["integration_id"], c) not in linked]
        if len(candidates) == 1:
            out[ref["id"]] = next(d for d in devices if d["id"] == candidates[0])
    return out


def link_matches(conn, user="system", ip=""):
    """Przypina wszystkie jednoznaczne dopasowania naraz. Zwraca ich liczbę."""
    matches = suggest_matches(conn)
    taken = set()
    for ref_id, dev in matches.items():
        if dev["id"] in taken:          # dwa obiekty wskazują to samo urządzenie — zostawiamy do decyzji
            continue
        taken.add(dev["id"])
        link(conn, ref_id, dev["id"], user=user, ip=ip)
    return len(taken)


def apply_choices(conn, choices, user="system", ip="", now=None):
    """Decyzje z kolejki naraz: {ref_id: "nowe" | "pomin" | id urządzenia do przypięcia}.
    Najpierw przypięcia i pominięcia, potem nowe — żeby zatwierdzenie z powiązanymi nie wciągnęło
    obiektu, o którym zdecydowano inaczej. Zwraca liczbę wykonanych decyzji."""
    done = 0
    for ref_id, what in choices.items():
        if _get(conn, ref_id)["status"] != "nowy":
            continue
        if what == "pomin":
            ignore(conn, ref_id, user=user, ip=ip)
        elif isinstance(what, int):
            link(conn, ref_id, what, user=user, ip=ip)
        else:
            continue
        done += 1
    for ref_id, what in choices.items():
        if what == "nowe" and _get(conn, ref_id)["status"] == "nowy":
            approve(conn, ref_id, user=user, ip=ip, now=now)
            done += 1
    return done


def ignore(conn, ref_id, user="system", ip=""):
    ref = _get(conn, ref_id)
    conn.execute(external_ref.update().where(external_ref.c.id == ref_id).values(status="ignorowany"))
    auth.audit(conn, "device_ignore", user=user, ip=ip, target=ref["proposed"]["name"],
               params={"external_id": ref["external_id"]})


# ───────────────────────── usunięte w źródle ─────────────────────────
def _removable():
    from .adapters import registry
    return {t: cls.removable for t, cls in registry().items() if cls.removable}


def _gone_query(conn, statuses):
    """(ref, typ integracji, nazwa integracji) obiektów z przedrostkiem `removable`, których dawno nie było w odczycie."""
    prefixes = _removable()
    rows = conn.execute(
        select(external_ref, integration.c.type.label("itype"), integration.c.name.label("iname"))
        .join(integration, integration.c.id == external_ref.c.integration_id)
        .where(external_ref.c.status.in_(statuses), integration.c.last_ok_at.isnot(None),
               external_ref.c.last_seen < integration.c.last_ok_at - GONE_AFTER)).mappings().all()
    return [r for r in rows if r["external_id"].startswith(prefixes.get(r["itype"], ()))]


def forget_gone(conn, integration_id):
    """Po udanym odczycie: czekające i pominięte obiekty, których już nie ma w źródle, znikają z kolejki
    (także powiązania po urządzeniu usuniętym wcześniej ręcznie)."""
    for r in _gone_query(conn, ("nowy", "ignorowany", "zatwierdzony")):
        if r["integration_id"] == integration_id and (r["status"] != "zatwierdzony" or r["device_id"] is None):
            _drop_ref(conn, r)


def gone(conn, device_id=None):
    """Zatwierdzone obiekty usunięte w źródle (do decyzji): ref + nazwa urządzenia i integracji."""
    rows = [dict(r) for r in _gone_query(conn, ("zatwierdzony",))
            if r["device_id"] and (device_id is None or r["device_id"] == device_id)]
    names = {d.id: d.name for d in conn.execute(select(device.c.id, device.c.name).where(
        device.c.id.in_([r["device_id"] for r in rows if r["device_id"]])))}
    for r in rows:
        r["device_name"] = names.get(r["device_id"])
    return rows


def _drop_ref(conn, ref):
    conn.execute(delete(observed_state).where(observed_state.c.integration_id == ref["integration_id"],
                                              observed_state.c.external_id == ref["external_id"]))
    conn.execute(delete(external_ref).where(external_ref.c.id == ref["id"]))


def resolve_gone(conn, choices, user="system", ip=""):
    """{ref_id: "usun" (urządzenie z Denali) | "zostaw" (urządzenie zostaje, bez powiązania)}. Zwraca liczbę decyzji."""
    current = {r["id"]: r for r in gone(conn)}
    done = 0
    for ref_id, what in choices.items():
        r = current.get(ref_id)
        if not r or what not in ("usun", "zostaw"):
            continue                    # wrócił w źródle albo już rozstrzygnięty
        _drop_ref(conn, r)
        params = {"external_id": r["external_id"], "integracja": r["iname"]}
        if what == "usun":
            inventory.delete_device(conn, r["device_id"])
            auth.audit(conn, "device_delete", user=user, ip=ip, target=r["device_name"] or r["external_id"],
                       params={**params, "powod": "usunięte w źródle"})
        else:
            auth.audit(conn, "device_unlink", user=user, ip=ip, target=r["device_name"] or r["external_id"],
                       params=params)
        done += 1
    return done
