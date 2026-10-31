"""Integracje: zapis konfiguracji (sekrety zaszyfrowane), odczyt przez adapter, zapis wyniku.

Wynik odczytu: nowe obiekty → kolejka „do zatwierdzenia”, stan → observed_state (tylko ostatni),
nowe adresy zatwierdzonych obiektów → adresy urządzenia (źródło: integracja), „przez co jest w sieci” → zależność
sieciowa odświeżana przy każdym odczycie.
"""
import asyncio
import json
import logging
import time

from sqlalchemy import delete, select

from . import discovery, inventory as inv
from .adapters import AdapterError, registry
from .models import dependency, external_ref, integration, observed_state, update_item

log = logging.getLogger("denali.integrations")
POLL_TIMEOUT = 90
MIN_POLL, MAX_POLL = 30, 3600


class IntegrationError(ValueError):
    pass


def adapter_class(type_):
    cls = registry().get(type_)
    if not cls:
        raise IntegrationError("Nieznany rodzaj integracji.")
    return cls


def split_form(cls, form, old_secrets=None):
    """Formularz → (config, secrets). Puste pole sekretu = bez zmiany."""
    config, secrets = {}, dict(old_secrets or {})
    for f in cls.fields:
        raw = form.get(f.key)
        if f.kind == "bool":
            config[f.key] = raw in ("1", "on", "tak", True)
        elif f.kind == "secret":
            if str(raw or "").strip():
                secrets[f.key] = str(raw).strip()
        else:
            config[f.key] = str(raw or "").strip()
    return config, secrets


def get(conn, integration_id):
    row = conn.execute(select(integration).where(integration.c.id == integration_id)).mappings().first()
    if not row:
        raise IntegrationError("Nie ma takiej integracji.")
    return row


def secrets_of(row, box):
    return json.loads(box.decrypt(row["secret_ct"])) if row["secret_ct"] else {}


def save(conn, box, type_, name, config, secrets, poll_seconds=None, integration_id=None):
    cls = adapter_class(type_)
    name = (name or "").strip() or cls.label
    if len(name) > 100:
        raise IntegrationError("Nazwa jest za długa.")
    try:
        cls.validate(config, secrets)
    except AdapterError as e:
        raise IntegrationError(str(e)) from None
    poll = min(MAX_POLL, max(MIN_POLL, int(poll_seconds or cls.default_poll)))
    values = {"type": type_, "name": name, "config": config, "secret_ct": box.encrypt(json.dumps(secrets)),
              "poll_seconds": poll}
    dup = conn.execute(select(integration.c.id).where(integration.c.name == name)).first()
    if dup and dup.id != integration_id:
        raise IntegrationError("Integracja o tej nazwie już istnieje.")
    if integration_id is None:
        return conn.execute(integration.insert().values(**values)).inserted_primary_key[0]
    conn.execute(integration.update().where(integration.c.id == integration_id).values(**values))
    return integration_id


def set_enabled(conn, integration_id, enabled):
    conn.execute(integration.update().where(integration.c.id == integration_id).values(enabled=bool(enabled)))


def remove(conn, integration_id):
    """Usuwa integrację, jej stan i kolejkę. Zatwierdzone urządzenia zostają (tracą tylko powiązanie)."""
    conn.execute(delete(integration).where(integration.c.id == integration_id))


def adapter_for(row, box, transport=None):
    return adapter_class(row["type"])(row["config"] or {}, secrets_of(row, box), transport=transport)


# ───────────────────────── wynik odczytu ─────────────────────────
def record(conn, integration_id, result, now=None):
    now = now or time.time()
    for f in result.found:
        discovery.upsert(conn, integration_id, f.external_id, f.proposed, now)
    _sync_approved(conn, integration_id, result.found)
    _sync_uplinks(conn, integration_id, result.found)
    seen = set()
    for s in result.states:
        seen.add(s.external_id)
        values = {"state": s.state, "summary": (s.summary or "")[:300], "data": s.data or {}, "observed_at": now}
        updated = conn.execute(observed_state.update().where(
            observed_state.c.integration_id == integration_id, observed_state.c.external_id == s.external_id
        ).values(**values)).rowcount
        if not updated:
            conn.execute(observed_state.insert().values(integration_id=integration_id, external_id=s.external_id,
                                                        **values))
    for s in result.states:
        if s.attach:
            _attach(conn, integration_id, s, now)
    # obiekty, których źródło już nie zgłasza (np. usunięta VM) — nie wiemy, co z nimi
    for row in conn.execute(select(observed_state.c.external_id, observed_state.c.state).where(
            observed_state.c.integration_id == integration_id)).all():
        if row.external_id not in seen and row.state != "unknown":
            conn.execute(observed_state.update().where(
                observed_state.c.integration_id == integration_id, observed_state.c.external_id == row.external_id
            ).values(state="unknown", summary="nie ma go już w źródle", observed_at=now))
    if result.updates is not None:
        record_updates(conn, integration_id, result.updates, now)
    conn.execute(integration.update().where(integration.c.id == integration_id).values(
        last_ok_at=now, last_attempt_at=now, last_error=""))


def record_updates(conn, integration_id, updates, now):
    """Lista aktualizacji źródła = dokładnie to, co podało teraz. „Od kiedy” zostaje, dopóki wersja się nie zmieni."""
    old = {(r.external_id, r.component): r for r in conn.execute(
        select(update_item).where(update_item.c.integration_id == integration_id))}
    conn.execute(delete(update_item).where(update_item.c.integration_id == integration_id))
    seen = set()
    for u in updates:
        key = (str(u.external_id)[:200], str(u.component)[:200])
        if key in seen:
            continue
        seen.add(key)
        prev = old.get(key)
        available = str(u.available or "")[:100]
        conn.execute(update_item.insert().values(
            integration_id=integration_id, external_id=key[0], component=key[1], kind=u.kind,
            current=str(u.current or "")[:100], available=available, security=bool(u.security),
            detected_at=prev.detected_at if prev and prev.available == available else now))


def record_error(conn, integration_id, message, now=None):
    now = now or time.time()
    conn.execute(integration.update().where(integration.c.id == integration_id).values(
        last_attempt_at=now, last_error=str(message)[:500]))


def _resolve_attach(conn, integration_id, attach):
    """Urządzenie, pod które podpiąć stan, albo None (np. VM jeszcze niezatwierdzona — spróbujemy następnym razem)."""
    q = select(external_ref.c.device_id).where(external_ref.c.status == "zatwierdzony",
                                               external_ref.c.device_id.isnot(None))
    if attach.get("ref"):
        q = q.where(external_ref.c.integration_id == integration_id, external_ref.c.external_id == attach["ref"])
    elif attach.get("integration_type") and attach.get("external_ids"):
        q = q.join(integration, integration.c.id == external_ref.c.integration_id).where(
            integration.c.type == attach["integration_type"], external_ref.c.external_id.in_(attach["external_ids"]))
    else:
        return None
    row = conn.execute(q.limit(1)).first()
    return row.device_id if row else None


def _attach(conn, integration_id, state, now):
    device_id = _resolve_attach(conn, integration_id, state.attach)
    if not device_id:
        return
    proposed = {"name": state.label or state.external_id, "kind": "inne", "addresses": [], "attached": True}
    row = conn.execute(select(external_ref.c.id, external_ref.c.device_id).where(
        external_ref.c.integration_id == integration_id, external_ref.c.external_id == state.external_id)).first()
    if row:
        if row.device_id != device_id:
            conn.execute(external_ref.update().where(external_ref.c.id == row.id).values(
                device_id=device_id, status="zatwierdzony", proposed=proposed, last_seen=now))
        return
    conn.execute(external_ref.insert().values(integration_id=integration_id, external_id=state.external_id,
                                              device_id=device_id, status="zatwierdzony", proposed=proposed,
                                              first_seen=now, last_seen=now))


def _sync_approved(conn, integration_id, found):
    """Nowy adres zgłoszony dla zatwierdzonego obiektu trafia do urządzenia (nic nie jest usuwane)."""
    by_id = {f.external_id: f for f in found}
    refs = conn.execute(select(external_ref.c.external_id, external_ref.c.device_id).where(
        external_ref.c.integration_id == integration_id, external_ref.c.status == "zatwierdzony",
        external_ref.c.device_id.isnot(None))).all()
    for ref in refs:
        f = by_id.get(ref.external_id)
        if not f or not inv.get_device(conn, ref.device_id):
            continue
        for a in f.proposed.get("addresses") or []:
            try:
                inv.add_address(conn, ref.device_id, a["kind"], a["value"], source="integracja")
            except (inv.InventoryError, KeyError):
                continue


def uplink_source(integration_id):
    return f"siec:{integration_id}"


def _sync_uplinks(conn, integration_id, found):
    """Zależności „w sieci przez” (np. klient Wi-Fi → AP) dokładnie takie, jak w ostatnim odczycie tej integracji.

    Urządzenie, którego źródło już nie widzi (przeszło do innego AP, wyłączone), traci zależność od tego źródła.
    Odczyt z błędem nie zmienia nic — gdy padnie AP, zależności zostają i analiza wskaże AP jako przyczynę.
    Zależności wpisanej ręcznie (albo z innego źródła) między tymi samymi urządzeniami nie ruszamy.
    """
    source = uplink_source(integration_id)
    refs = {r.external_id: r.device_id for r in conn.execute(
        select(external_ref.c.external_id, external_ref.c.device_id).where(
            external_ref.c.integration_id == integration_id, external_ref.c.status == "zatwierdzony",
            external_ref.c.device_id.isnot(None)))}
    wanted = {}
    for f in found:
        up = f.proposed.get("uplink")
        if not up:
            continue
        # znane urządzenie po MAC, nawet jeśli to źródło widzi je pierwszy raz (telefon przeszedł do innego AP)
        dev = refs.get(f.external_id) or next(
            (d for d in (inv.find_by_address(conn, "mac", a["value"]) for a in f.proposed.get("addresses") or []
                         if a.get("kind") == "mac") if d), None)
        if not dev:
            continue
        target = discovery._target_device(conn, integration_id, up)
        if target and target != dev:
            wanted[(dev, target)] = bool(up.get("hard", True))
    for d in conn.execute(select(dependency).where(dependency.c.source == source)).mappings().all():
        key = (d["device_id"], d["depends_on_id"])
        if key not in wanted:
            conn.execute(dependency.delete().where(dependency.c.id == d["id"]))
        elif d["hard"] != wanted[key]:
            conn.execute(dependency.update().where(dependency.c.id == d["id"]).values(hard=wanted[key]))
        wanted.pop(key, None)
    for (dev, target), hard in wanted.items():
        exists = conn.execute(select(dependency.c.id).where(
            dependency.c.device_id == dev, dependency.c.depends_on_id == target, dependency.c.type == "siec")).first()
        if exists:
            continue
        try:
            inv.add_dependency(conn, dev, target, "siec", hard=hard, source=source)
        except inv.InventoryError:
            continue    # pętla (np. dwa routery widzą się nawzajem jako klientów) — pomijamy


# ───────────────────────── odczyt ─────────────────────────
def due(conn, now=None):
    now = now or time.time()
    rows = conn.execute(select(integration).where(integration.c.enabled.is_(True))).mappings().all()
    return [r for r in rows if not r["last_attempt_at"] or now - r["last_attempt_at"] >= r["poll_seconds"]]


async def poll_one(engine, box, row, transport=None, now=None):
    """Odczytuje jedną integrację i zapisuje wynik albo błąd. Zwraca None albo opis błędu."""
    try:
        result = await asyncio.wait_for(adapter_for(row, box, transport).poll(), POLL_TIMEOUT)
    except AdapterError as e:
        error = str(e)
    except asyncio.TimeoutError:
        error = f"Odczyt trwał dłużej niż {POLL_TIMEOUT} s."
    except Exception as e:     # błąd w adapterze nie może zatrzymać workera
        log.exception("Integracja %s: nieoczekiwany błąd", row["name"])
        error = f"Nieoczekiwany błąd: {e.__class__.__name__}"
    else:
        with engine.begin() as conn:
            record(conn, row["id"], result, now)
        return None
    with engine.begin() as conn:
        record_error(conn, row["id"], error, now)
    return error


async def poll_due(engine, box, now=None):
    with engine.connect() as conn:
        rows = due(conn, now)
    for row in rows:
        await poll_one(engine, box, row, now=now)
    return len(rows)


def states_for_device(conn, device_id):
    """Stan ze wszystkich źródeł, które zgłaszają to urządzenie (do karty urządzenia)."""
    q = (select(observed_state, integration.c.name.label("source"), integration.c.type, external_ref.c.proposed)
         .join(external_ref, (external_ref.c.integration_id == observed_state.c.integration_id)
               & (external_ref.c.external_id == observed_state.c.external_id))
         .join(integration, integration.c.id == observed_state.c.integration_id)
         .where(external_ref.c.device_id == device_id))
    return conn.execute(q).mappings().all()
