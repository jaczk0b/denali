"""Kolejka „do zatwierdzenia”: obiekty zgłoszone przez integracje czekają, aż jednym kliknięciem staną się urządzeniami.

Integracja opisuje obiekt słownikiem `proposed`:
  name, kind, app_url (opcjonalnie)
  addresses   [{"kind": ip|mac|hostname, "value": …}]
  parent      na czym działa: {"ref": external_id tej samej integracji} albo {"ip"|"mac"|"hostname": …}
  depends_on  [{"ref"|"ip"|"mac"|"hostname": …, "type": dane|siec|zasilanie|usluga, "hard": bool}]

Adres pozwala połączyć obiekty z różnych integracji: stack z Komodo trafia na VM z Proxmoxa o tym samym IP.
Zatwierdzenie jednego obiektu zatwierdza też czekające obiekty, na których działa, i te, które działają na nim —
nowa VM ze stackami to jedno kliknięcie.
"""
import time

from sqlalchemy import select

from . import auth, inventory
from .models import external_ref

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


def ignore(conn, ref_id, user="system", ip=""):
    ref = _get(conn, ref_id)
    conn.execute(external_ref.update().where(external_ref.c.id == ref_id).values(status="ignorowany"))
    auth.audit(conn, "device_ignore", user=user, ip=ip, target=ref["proposed"]["name"],
               params={"external_id": ref["external_id"]})
