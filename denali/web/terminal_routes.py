"""Karta Terminale (tylko administrator): terminal SSH, ekran VM i LXC, gotowe polecenia przez agenta, historia sesji.

O tym, czy wolno, decydują consoles.request, consoles.request_ssh i actions.request. WebSocket nie przechodzi przez require_admin, więc
sprawdza sam: Origin (ciasteczko poszłoby też z obcej strony), sesję i to, czy konsolę zgłosił ten sam użytkownik.
"""
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select

from .. import actions, auth, consoles, inventory as inv, polecenia, ssh, vault
from ..adapters import AdapterError
from ..db import kv_get
from ..models import action_run, setting
from ..templating import render
from .deps import COOKIE, CSP_XTERM, NotFound, client_ip, require_admin, require_fresh

router = APIRouter()
log = logging.getLogger("denali.consoles")
COMMAND_KEY = "agent.polecenia"


def _transport(request):
    return getattr(request.app.state, "adapter_transport", None)   # w testach: nagrane odpowiedzi API


def _device_or_404(conn, device_id):
    d = inv.get_device(conn, device_id)
    if not d:
        raise NotFound("Nie ma takiego urządzenia.")
    return d


@router.get("/terminale")
def terminals(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        rows = consoles.devices(conn)
        history = consoles.recent(conn)
        scopes = consoles.scopes(conn)
        custom = polecenia.custom_text(conn)
    return render(request, "terminale.html", {"devices": rows, "history": history, "scopes": scopes, "custom": custom})


@router.post("/terminale/polecenia")
async def terminal_presets(request: Request, sess: dict = Depends(require_admin)):
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        polecenia.save_custom(conn, form.get("presets"))
        auth.audit(conn, "terminal_presets", user=sess["login"], ip=client_ip(request),
                   params={"ile": len(polecenia.custom(conn))})
    return RedirectResponse("/terminale", 303)


@router.get("/terminale/{device_id:int}")
def terminal_device(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        d = _device_or_404(conn, device_id)
        opts = actions.for_device(conn, device_id, terminal=True)
        runs = conn.execute(select(action_run).where(action_run.c.device_id == device_id, action_run.c.action == COMMAND_KEY)
                            .order_by(action_run.c.id.desc()).limit(10)).mappings().all()
        history = consoles.recent(conn, device_id, 10)
        hosts = consoles.addresses(conn, device_id) if d["kind"] in consoles.SSH_KINDS else []
        known = [h for h in hosts if kv_get(conn, ssh.key_name(h, 22))]
        secrets = vault.list_secrets(conn, role=sess["role"], device_id=device_id)
    return render(request, "terminal.html", {
        "hosts": hosts, "known": known, "secrets": secrets,
        "d": d, "console": [o for o in opts if o["key"] == consoles.KEY], "agents": [o for o in opts if o["key"] == COMMAND_KEY],
        "commands": polecenia.COMMANDS, "runs": runs, "history": history,
        "active": any(r["status"] in actions.ACTIVE for r in runs), "labels": actions.STATUS_LABEL})


@router.post("/terminale/{device_id:int}/polecenie")
async def terminal_command(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    form = await request.form()
    try:
        ref_id = int(form.get("ref_id") or 0)
    except ValueError:
        ref_id = 0
    with request.app.state.engine.begin() as conn:
        _device_or_404(conn, device_id)
        try:
            actions.request(conn, device_id=device_id, ref_id=ref_id, key=COMMAND_KEY, user=sess["login"], role=sess["role"],
                            ip=client_ip(request), command=str(form.get("command") or ""))
            error = None
        except actions.ActionError as e:
            error = str(e)
    if error:
        return render(request, "blad.html", {"title": "Polecenie niemożliwe", "text": error,
                                             "back": f"/terminale/{device_id}"}, 403)
    return RedirectResponse(f"/terminale/{device_id}", 303)


@router.post("/terminale/{device_id:int}/ssh")
async def ssh_open(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)
    form = await request.form()
    app, ip = request.app, client_ip(request)
    consoles.forget_unused(app)
    host, login = str(form.get("host") or ""), str(form.get("login") or "").strip()
    secret = str(form.get("key") or "") or str(form.get("password") or "")
    try:
        port = int(form.get("port") or 22)
    except ValueError:
        port = 0
    with app.state.engine.begin() as conn:
        try:
            if form.get("secret_id"):
                row = next((s for s in vault.list_secrets(conn, role=sess["role"], device_id=device_id)
                            if str(s["id"]) == form.get("secret_id")), None)
                if not row:
                    raise consoles.ConsoleError("Tego wpisu nie ma w Skarbcu tego urządzenia.")
                login = login or row["username"]
                secret = vault.reveal(conn, app.state.box, row["id"], role=sess["role"], reauth_at=sess["reauth_at"],
                                      user=sess["login"], ip=ip)
            if not secret:
                raise consoles.ConsoleError("Podaj hasło albo wybierz wpis ze Skarbca.")
            console_id = consoles.request_ssh(conn, device_id=device_id, host=host, port=port, login=login, user=sess["login"],
                                              role=sess["role"], ip=ip, reauth_ok=True)
            error = None
        except (consoles.ConsoleError, vault.VaultError, ValueError) as e:
            error = str(e)
    if error:
        return render(request, "blad.html", {"title": "Terminal niedostępny", "text": error,
                                             "back": f"/terminale/{device_id}"}, 403)
    app.state.console_tickets[console_id] = {"adapter": ssh.target(app.state.engine, host, port, login, secret),
                                             "info": None, "at": time.time()}
    return RedirectResponse(f"/terminale/konsola/{console_id}", 303)


@router.post("/terminale/{device_id:int}/ssh/zapomnij")
async def ssh_forget_key(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)
    form = await request.form()
    host = str(form.get("host") or "")
    with request.app.state.engine.begin() as conn:
        d = _device_or_404(conn, device_id)
        if host not in consoles.addresses(conn, device_id):
            raise NotFound("Tego adresu nie ma na karcie urządzenia.")
        conn.execute(delete(setting).where(setting.c.key == ssh.key_name(host, 22)))
        auth.audit(conn, "ssh_key_forget", user=sess["login"], ip=client_ip(request), target=d["name"], params={"host": host})
    return RedirectResponse(f"/terminale/{device_id}", 303)


@router.post("/terminale/{device_id:int}/konsola/{ref_id:int}")
async def console_request(request: Request, device_id: int, ref_id: int, sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)
    with request.app.state.engine.begin() as conn:
        try:
            console_id = consoles.request(conn, device_id=device_id, ref_id=ref_id, user=sess["login"], role=sess["role"],
                                          ip=client_ip(request), reauth_ok=True)
            error = None
        except consoles.ConsoleError as e:
            error = str(e)
    if error:
        return render(request, "blad.html", {"title": "Konsola niedostępna", "text": error,
                                             "back": f"/terminale/{device_id}"}, 403)
    return RedirectResponse(f"/terminale/konsola/{console_id}", 303)


@router.get("/terminale/konsola/{console_id:int}")
def console_page(request: Request, console_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        row = consoles.get(conn, console_id)
        presets = polecenia.presets(conn)
    if not row or row["user"] != sess["login"]:
        raise NotFound("Nie ma takiej sesji.")
    if row["external_id"] == ssh.EXTERNAL_ID and not row["closed_at"] and console_id in request.app.state.console_tickets:
        resp = render(request, "ssh.html", {"row": row, "presets": presets})
        resp.headers["Content-Security-Policy"] = CSP_XTERM
        return resp
    ended = row["opened_at"] is not None or row["closed_at"] is not None
    return render(request, "konsola.html", {"row": row, "ended": ended})


@router.post("/terminale/konsola/{console_id:int}/start")
async def console_start(request: Request, console_id: int, sess: dict = Depends(require_admin)):
    app = request.app
    consoles.forget_unused(app)
    with app.state.engine.begin() as conn:
        try:
            row, integ, data = consoles.claim(conn, console_id, sess["login"])
        except consoles.ConsoleError as e:
            return JSONResponse({"error": str(e)}, 409)
    try:
        adapter = actions.adapter_for(integ, app.state.box, _transport(request))
        info = await adapter.console_ticket(row["external_id"], data)
    except AdapterError as e:
        with app.state.engine.begin() as conn:
            consoles.close(conn, row, str(e))
        return JSONResponse({"error": str(e)}, 502)
    app.state.console_tickets[console_id] = {"adapter": adapter, "info": info, "at": time.time()}
    return {"password": info["password"]}


@router.get("/terminale/konsola/{console_id:int}/koniec")
def console_end(request: Request, console_id: int, sess: dict = Depends(require_admin)):
    """Dlaczego sesja się skończyła — przeglądarka widzi tylko, że połączenie padło."""
    app = request.app
    with app.state.engine.begin() as conn:
        row = consoles.get(conn, console_id)
        if not row or row["user"] != sess["login"]:
            raise NotFound("Nie ma takiej sesji.")
        if app.state.console_tickets.pop(console_id, None):     # bilet czeka, a przeglądarka już zgłasza koniec
            consoles.close(conn, row, consoles.NO_WEBSOCKET)
        row = consoles.get(conn, console_id)
    return {"reason": row["close_reason"]}


def _same_origin(ws):
    origin = urlsplit(ws.headers.get("origin", "")).netloc
    base = urlsplit(ws.app.state.settings.base_url or "").netloc
    return bool(origin) and origin in (ws.headers.get("host", ""), base)


@router.websocket("/terminale/konsola/{console_id:int}/ws")
async def console_ws(ws: WebSocket, console_id: int):
    app = ws.app
    token, ip = ws.cookies.get(COOKIE, ""), ws.client.host if ws.client else ""
    with app.state.engine.begin() as conn:
        sess = auth.load_session(conn, token, ip, touch=False)
        row = consoles.get(conn, console_id)
    if (not _same_origin(ws) or not sess or sess["kind"] != "zwykla" or sess["role"] != "admin" or not row
            or row["user"] != sess["login"] or console_id not in app.state.console_tickets):
        log.warning("Odrzucony WebSocket konsoli %s: origin=%r host=%r sesja=%s", console_id, ws.headers.get("origin"),
                    ws.headers.get("host"), bool(sess))
        await ws.close(4403)        # przed accept: przeglądarka widzi odmowę połączenia
        return
    pending = app.state.console_tickets.pop(console_id)
    await ws.accept()
    try:
        upstream = await pending["adapter"].console_connect(pending["info"])
    except Exception as e:          # każdy błąd ma zostać w historii sesji, a nie zniknąć razem z połączeniem
        if not isinstance(e, AdapterError):
            log.exception("Połączenie z konsolą w Proxmoxie nie powiodło się")
        with app.state.engine.begin() as conn:
            consoles.close(conn, row, str(e) if isinstance(e, AdapterError) else f"Błąd połączenia: {e.__class__.__name__}: {e}")
        await ws.close(4502)
        return

    def check():
        with app.state.engine.begin() as conn:
            return consoles.why_stop(conn, row, token, ip)

    reason = await consoles.bridge(ws, upstream, check)
    with app.state.engine.begin() as conn:
        consoles.close(conn, row, reason)
    try:
        await ws.close(1000)
    except RuntimeError:            # przeglądarka już się rozłączyła
        pass
