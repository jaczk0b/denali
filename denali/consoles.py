"""Sesje z karty Terminale (Etap 6): ekran VM i LXC przez API Proxmoxa oraz terminal SSH (ssh.py), bez kluczy w bazie.

Otwarcie przechodzi przez zamki akcji (actions.for_device z terminal=True): wyłącznik akcji, zakres „Konsola”
integracji, stan maszyny — a do tego świeże potwierdzenie hasłem i kodem. Profile nie mają tu nic do powiedzenia:
zakres dotyczy wszystkich VM i LXC tego Proxmoxa.

Przebieg: zgłoszenie (formularz) → strona konsoli → start (bilet od Proxmoxa, raz) → WebSocket przeglądarki, który panel
przekazuje do Proxmoxa. Działająca sesja jest sprawdzana co CHECK_EVERY sekund: wylogowanie, wyłączenie akcji, odebranie
zakresu albo koniec czasu zamykają ją, nie czekając na przeglądarkę. Otwarcie, odmowa i koniec trafiają do audytu.
Treści sesji nie zapisujemy — to obraz, a przez niego idzie też hasło do maszyny.
"""
import asyncio
import logging
import re
import time

from sqlalchemy import select

from . import actions, auth, ssh, tablet
from .adapters import registry
from .db import kv_get, kv_set
from .models import console_session, device, device_address, integration, observed_state

log = logging.getLogger("denali.consoles")
KEY = "proxmox.console"
TABS = ("ssh", "vnc")
HIDDEN_KEY = "terminal.hidden."
SSH_KINDS = ("host", "vm", "lxc", "vps", "nas", "komputer")     # tam zwykle jest SSH
LOGIN = re.compile(r"^[\w.@-]{1,64}$")
START_WITHIN = 60       # tyle po zgłoszeniu strona może zacząć połączenie
TICKET_VALID = 15       # bilet Proxmoxa jest ważny krótko: bez połączenia przeglądarki sesja się kończy
NO_WEBSOCKET = "Połączenie WebSocket nie dotarło do panelu: włącz Websockets Support w Nginx Proxy Manager."
MAX_LIFE = 2 * 3600     # dłużej nie ma sesji bez ponownego potwierdzenia
CHECK_EVERY = 15


class ConsoleError(ValueError):
    pass


def _switched_on(o):
    if o["cls"].push:
        return o["spec"].key in ((o["state"]["data"] if o["state"] else {}) or {}).get("actions", [])
    return actions.has_credentials(o["integration"], o["spec"])


def devices(conn):
    """Urządzenia z terminalem, konsolą albo poleceniami (lista na karcie Terminale). Wyłączona maszyna też tu jest —
    jej strona powie, dlaczego teraz się nie da."""
    out = []
    for r in conn.execute(select(device.c.id, device.c.name, device.c.kind, device.c.icon).order_by(device.c.name)).all():
        opts = [o for o in actions.for_device(conn, r.id, terminal=True) if _switched_on(o)]
        terminal = r.kind in SSH_KINDS and bool(addresses(conn, r.id))
        if opts or terminal:
            logins = ssh.saved_logins(conn, r.id) if terminal else []
            out.append({"id": r.id, "name": r.name, "kind": r.kind, "icon": r.icon, "terminal": terminal,
                        "console": [o for o in opts if o["key"] == KEY],
                        "login": logins[0] if logins else None, "host": (addresses(conn, r.id) or [""])[0],
                        "commands": [o for o in opts if o["key"] != KEY]})
    return out


def hidden(conn, tab):
    """Urządzenia ukryte na zakładce: maszyna potrzebna po SSH może być zbędna w VNC."""
    return kv_get(conn, HIDDEN_KEY + tab, [])


def set_hidden(conn, tab, device_id, hide):
    ids = set(hidden(conn, tab)) - {device_id}
    kv_set(conn, HIDDEN_KEY + tab, sorted(ids | {device_id}) if hide else sorted(ids))


def addresses(conn, device_id):
    """Adresy, na które wolno otworzyć SSH — tylko z karty urządzenia, panel nie łączy się pod dowolny adres."""
    return [r.value for r in conn.execute(
        select(device_address.c.value).where(device_address.c.device_id == device_id,
                                             device_address.c.kind.in_(("ip", "hostname")))
        .order_by(device_address.c.is_primary.desc(), device_address.c.id))]


def scopes(conn):
    """Integracje z zakresami i to, co jest w nich naprawdę zapisane — pusta karta mówi, czego brakuje."""
    rows = conn.execute(select(integration).order_by(integration.c.name)).mappings().all()
    types = registry()
    return [{"id": r["id"], "name": r["name"], "console": actions.has_credentials(r, next(
        a for a in types[r["type"]].actions if a.grant == "konsola"))}
        for r in rows if r["type"] in types and types[r["type"]].grants]


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


def request_ssh(conn, *, device_id, host, port, login, user, role, ip="", reauth_ok=False, now=None):
    """Sesja SSH po sprawdzeniu zamków. Odmowa → ConsoleError i wpis „console_blocked” w audycie."""
    now = now or time.time()
    dev = conn.execute(select(device.c.id, device.c.name).where(device.c.id == device_id)).first()

    def refuse(why):
        auth.audit(conn, "console_blocked", user=user, ip=ip, target=dev.name if dev else str(device_id),
                   params={"host": str(host)[:100], "login": str(login)[:64]}, result=why)
        raise ConsoleError(why)

    if role != "admin":
        refuse("Terminal może otworzyć tylko administrator.")
    if not dev:
        refuse("Nie ma takiego urządzenia.")
    if not actions.enabled(conn):
        refuse("Akcje są wyłączone (Akcje → Włącz).")
    if not reauth_ok:
        refuse("Terminal wymaga potwierdzenia hasłem i kodem 2FA.")
    if host not in addresses(conn, device_id):
        refuse("Tego adresu nie ma na karcie urządzenia.")
    if not 1 <= port <= 65535:
        refuse("Zły port.")
    if not LOGIN.match(login):
        refuse("Zły login.")
    console_id = conn.execute(console_session.insert().values(
        created_at=now, device_id=device_id, external_id=ssh.EXTERNAL_ID, target=dev.name, user=user, ip=ip,
        opened_at=now)).inserted_primary_key[0]
    auth.audit(conn, "console_open", user=user, ip=ip, target=dev.name,
               params={"session": console_id, "external_id": ssh.EXTERNAL_ID, "host": host, "port": port, "login": login})
    return console_id


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
    sess = tablet.resolve(conn, auth.load_session(conn, token, ip))
    if not sess or not (sess["kind"] == "zwykla" or sess.get("elevated")) or sess["role"] != "admin":
        return "Logowanie wygasło, zostało cofnięte albo tablet się zablokował."
    if not actions.enabled(conn):
        return "Akcje wyłączono."
    if not row["device_id"]:
        return "Urządzenie zostało usunięte."
    if row["external_id"] != ssh.EXTERNAL_ID:
        integ = conn.execute(select(integration).where(integration.c.id == row["integration_id"])).mappings().first()
        if not integ or "konsola" not in ((integ["config"] or {}).get("permissions") or []):
            return "Integracja nie ma już zakresu „Konsola”."
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
                    close(conn, row, NO_WEBSOCKET, now)


async def bridge(ws, upstream, check):
    """Przekazuje ramki w obie strony, aż któraś strona skończy albo check() poda powód zamknięcia. Zwraca powód."""
    async def to_browser():
        async for m in upstream:
            await (ws.send_bytes(m) if isinstance(m, bytes) else ws.send_text(m))
        code, why = getattr(upstream, "close_code", None), getattr(upstream, "close_reason", "")
        return "Proxmox zakończył konsolę" + (f" (kod {code}{': ' + why if why else ''})." if code not in (None, 1000) else ".")

    async def to_upstream():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                code = m.get("code")
                return "Rozłączono." if code in (None, 1000, 1001, 1005) else f"Przeglądarka straciła połączenie (kod {code})."
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
    if e := finished.exception():
        log.warning("Konsola przerwana: %r", e)
        return f"Połączenie przerwane ({e.__class__.__name__}: {e})"[:200]
    return finished.result()
