"""Konsola maszyny z karty Terminale (Etap 6): ekran VM i LXC przez API Proxmoxa, bez kluczy SSH w bazie.

Otwarcie przechodzi przez te same zamki co akcja (actions.for_device z terminal=True): wyłącznik akcji, „Dozwolone
akcje” urządzenia („proxmox.console” z profilu albo wyjątek), zakres „Konsola” integracji, stan maszyny — a do tego
świeże potwierdzenie hasłem i kodem.

Przebieg: zgłoszenie (formularz) → strona konsoli → start (bilet od Proxmoxa, raz) → WebSocket przeglądarki, który panel
przekazuje do Proxmoxa. Działająca sesja jest sprawdzana co CHECK_EVERY sekund: wylogowanie, wyłączenie akcji, odebranie
zgody albo koniec czasu zamykają ją, nie czekając na przeglądarkę. Otwarcie, odmowa i koniec trafiają do audytu.
Treści sesji nie zapisujemy — to obraz, a przez niego idzie też hasło do maszyny.
"""
import asyncio
import logging
import time

from sqlalchemy import select

from . import actions, auth, profiles
from .models import console_session, device, external_ref, integration, observed_state

log = logging.getLogger("denali.consoles")
KEY = "proxmox.console"
START_WITHIN = 60       # tyle po zgłoszeniu strona może zacząć połączenie
TICKET_VALID = 15       # bilet Proxmoxa jest ważny krótko: bez połączenia przeglądarki sesja się kończy
MAX_LIFE = 2 * 3600     # dłużej nie ma sesji bez ponownego potwierdzenia
CHECK_EVERY = 15


class ConsoleError(ValueError):
    pass


def devices(conn):
    """Urządzenia, na których konsola albo polecenia są dozwolone (lista na karcie Terminale)."""
    rows = conn.execute(select(device.c.id, device.c.name).where(device.c.id.in_(
        select(external_ref.c.device_id).where(external_ref.c.status == "zatwierdzony"))).order_by(device.c.name)).all()
    out = []
    for r in rows:
        opts = [o for o in actions.for_device(conn, r.id, terminal=True) if o["allowed"]]
        if opts:
            out.append({"id": r.id, "name": r.name, "console": [o for o in opts if o["key"] == KEY],
                        "commands": [o for o in opts if o["key"] != KEY]})
    return out


def get(conn, console_id):
    return conn.execute(select(console_session).where(console_session.c.id == console_id)).mappings().first()


def recent(conn, device_id=None, limit=20):
    q = select(console_session).where(console_session.c.opened_at.is_not(None))
    if device_id is not None:
        q = q.where(console_session.c.device_id == device_id)
    return conn.execute(q.order_by(console_session.c.id.desc()).limit(limit)).mappings().all()


def request(conn, *, device_id, ref_id, user, role, ip="", reauth_ok=False, now=None):
    """Zgłoszenie konsoli po sprawdzeniu zamków. Odmowa → ConsoleError i wpis „console_blocked” w audycie."""
    now = now or time.time()
    dev = conn.execute(select(device.c.id, device.c.name).where(device.c.id == device_id)).first()

    def refuse(why):
        auth.audit(conn, "console_blocked", user=user, ip=ip, target=dev.name if dev else str(device_id),
                   params={"ref_id": ref_id}, result=why)
        raise ConsoleError(why)

    if role != "admin":
        refuse("Konsolę może otworzyć tylko administrator.")
    if not dev:
        refuse("Nie ma takiego urządzenia.")
    o = next((x for x in actions.for_device(conn, device_id, terminal=True)
              if x["ref_id"] == ref_id and x["key"] == KEY), None)
    if not o:
        refuse("Tej konsoli nie ma dla tego urządzenia.")
    if o["reason"]:
        refuse(o["reason"])
    if not reauth_ok:
        refuse("Konsola wymaga potwierdzenia hasłem i kodem 2FA.")
    return conn.execute(console_session.insert().values(
        created_at=now, device_id=device_id, integration_id=o["integration"]["id"], external_id=o["external_id"],
        target=dev.name, user=user, ip=ip)).inserted_primary_key[0]


def claim(conn, console_id, user, now=None):
    """Początek połączenia — tylko raz, tylko przez tego, kto zgłosił, i tylko zaraz po zgłoszeniu.
    Zwraca (sesja, integracja, dane obiektu z ostatniego odczytu)."""
    now = now or time.time()
    if not actions.enabled(conn):
        raise ConsoleError("Akcje są wyłączone.")
    if not conn.execute(console_session.update().where(
            console_session.c.id == console_id, console_session.c.user == user,
            console_session.c.opened_at.is_(None), console_session.c.closed_at.is_(None),
            console_session.c.created_at > now - START_WITHIN).values(opened_at=now)).rowcount:
        raise ConsoleError("Ta sesja już się skończyła — otwórz konsolę jeszcze raz.")
    row = get(conn, console_id)
    integ = conn.execute(select(integration).where(integration.c.id == row["integration_id"])).mappings().first()
    if not integ:
        close(conn, row, "Integracja została usunięta.", now)
        raise ConsoleError("Integracja została usunięta.")
    st = conn.execute(select(observed_state.c.data).where(observed_state.c.integration_id == integ["id"],
                                                          observed_state.c.external_id == row["external_id"])).first()
    auth.audit(conn, "console_open", user=user, ip=row["ip"], target=row["target"],
               params={"session": console_id, "external_id": row["external_id"]})
    return row, integ, st.data if st else {}


def close(conn, row, reason, now=None):
    now = now or time.time()
    if conn.execute(console_session.update().where(console_session.c.id == row["id"], console_session.c.closed_at.is_(None))
                    .values(closed_at=now, close_reason=str(reason)[:200])).rowcount:
        auth.audit(conn, "console_close", user=row["user"], ip=row["ip"], target=row["target"],
                   params={"session": row["id"], "seconds": round(now - (row["opened_at"] or now))}, result=reason)


def why_stop(conn, row, token, ip, now=None):
    """Powód zamknięcia działającej sesji albo None. Otwarta konsola to aktywność, więc logowanie się nie przedawnia."""
    now = now or time.time()
    sess = auth.load_session(conn, token, ip)
    if not sess or sess["kind"] != "zwykla" or sess["role"] != "admin":
        return "Logowanie wygasło albo zostało cofnięte."
    if not actions.enabled(conn):
        return "Akcje wyłączono."
    if not row["device_id"] or KEY not in profiles.value(conn, row["device_id"], "actions"):
        return "Konsola nie jest już dozwolona na tym urządzeniu."
    if now - (row["opened_at"] or now) > MAX_LIFE:
        return "Koniec czasu sesji (2 godziny)."
    return None


def forget_unused(app, now=None):
    """Bilety, po które przeglądarka nie przyszła: kończą sesję, zamiast wisieć w pamięci."""
    now = now or time.time()
    for cid, pending in list(app.state.console_tickets.items()):
        if now - pending["at"] > TICKET_VALID:
            del app.state.console_tickets[cid]
            with app.state.engine.begin() as conn:
                if row := get(conn, cid):
                    close(conn, row, "Przeglądarka nie połączyła się z konsolą.", now)


async def bridge(ws, upstream, check):
    """Przekazuje ramki w obie strony, aż któraś strona skończy albo check() poda powód zamknięcia. Zwraca powód."""
    async def to_browser():
        async for m in upstream:
            await (ws.send_bytes(m) if isinstance(m, bytes) else ws.send_text(m))
        return "Proxmox zakończył konsolę."

    async def to_upstream():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return "Rozłączono."
            data = m.get("bytes") if m.get("bytes") is not None else m.get("text")
            if data is not None:
                await upstream.send(data)

    async def watch():
        while True:
            await asyncio.sleep(CHECK_EVERY)
            if reason := check():
                return reason

    tasks = [asyncio.create_task(c()) for c in (to_browser, to_upstream, watch)]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    try:
        await upstream.close()
    except Exception:       # połączenie już padło — zostaje podać powód
        log.debug("Zamknięcie konsoli w Proxmoxie nie powiodło się", exc_info=True)
    finished = next(iter(done))
    if finished.exception():
        return "Połączenie przerwane."
    return finished.result()
