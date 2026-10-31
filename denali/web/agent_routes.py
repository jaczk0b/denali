"""Agent: plik do pobrania, rejestracja jednorazowym kodem, raporty (API bez sesji, z tokenem) i strony administratora.

API agenta nie używa ciasteczek ani CSRF — uwierzytelnia je token w nagłówku Authorization (Bearer).
Plik agenta jest publiczny w LAN (nie zawiera żadnych sekretów); polecenie instalacji sprawdza jego skrót SHA-256,
więc na maszynie uruchamiasz dokładnie to, co pokazał panel.
"""
import hashlib
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select

from .. import agents, auth, integrations
from ..models import external_ref, observed_state
from ..templating import render
from .deps import NotFound, client_ip, require_admin

router = APIRouter()
log = logging.getLogger("denali.agents")
AGENT_FILE = Path(__file__).resolve().parent.parent / "agent" / "denali-agent.py"
MAX_REPORT = 1024 * 1024
ENROLL_LOGIN = "#agent"          # nieudane rejestracje liczą się w tym samym limicie prób co logowanie (po IP)


def agent_source():
    return AGENT_FILE.read_bytes()


def agent_sha256():
    return hashlib.sha256(agent_source()).hexdigest()


def install_command(base_url, code):
    base = base_url.rstrip("/")
    return (f"curl -fsSLo denali-agent.py {base}/agent/denali-agent.py && "
            f"echo \"{agent_sha256()}  denali-agent.py\" | sha256sum -c && "
            f"sudo python3 denali-agent.py install {base} {code}")


@router.get("/agent/denali-agent.py")
def agent_file():
    return PlainTextResponse(agent_source(), media_type="text/x-python; charset=utf-8")


@router.get("/agent/denali-agent.py.sha256")
def agent_file_hash():
    return PlainTextResponse(f"{agent_sha256()}  denali-agent.py\n")


async def _json(request, limit):
    if int(request.headers.get("content-length") or 0) > limit:
        return None
    body = await request.body()
    if len(body) > limit:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _error(status, text):
    return JSONResponse({"error": text}, status)


@router.post("/api/agent/v1/rejestracja")
async def agent_enroll(request: Request):
    data = await _json(request, 16 * 1024)
    if not isinstance(data, dict):
        return _error(400, "Zły format zgłoszenia.")
    ip = client_ip(request)
    with request.app.state.engine.begin() as conn:
        if auth.login_blocked(conn, ENROLL_LOGIN, ip):
            return _error(429, "Za dużo nieudanych prób z tego adresu — odczekaj 15 minut.")
        try:
            iid, token = agents.enroll(conn, data.get("code"), data.get("hostname"))
        except agents.AgentError as e:
            auth.record_attempt(conn, ENROLL_LOGIN, ip, False)
            return _error(403, str(e))
        auth.record_attempt(conn, ENROLL_LOGIN, ip, True)
        auth.audit(conn, "agent_enroll", user="agent", ip=ip, target=str(data.get("hostname"))[:64],
                   params={"integration_id": iid})
    return {"token": token, "interval": agents.REPORT_EVERY}


@router.post("/api/agent/v1/raport")
async def agent_report(request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    data = await _json(request, MAX_REPORT)
    with request.app.state.engine.begin() as conn:
        row = agents.authenticate(conn, token)
        if not row:
            return _error(401, "Token nieważny — agent został usunięty w Denali albo wyłączony. Zainstaluj go ponownie.")
        if not isinstance(data, dict):
            return _error(400, "Zły format raportu.")
        try:
            agents.record_report(conn, row, data)
        except agents.AgentError as e:
            integrations.record_error(conn, row["id"], f"Raport odrzucony: {e}")
            return _error(400, str(e))
    return {"interval": agents.REPORT_EVERY}


# ───────────────────────── strony administratora ─────────────────────────
@router.get("/admin/agenci/nowy")
def agent_new(request: Request, sess: dict = Depends(require_admin)):
    return render(request, "agent_nowy.html", {"command": None})


@router.post("/admin/agenci/nowy")
def agent_new_code(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        code = agents.new_code(conn, sess["login"])
        auth.audit(conn, "agent_code", user=sess["login"], ip=client_ip(request))
    return render(request, "agent_nowy.html", {
        "command": install_command(request.app.state.settings.base_url, code), "code": code})


def agent_page(request, conn, row):
    state = conn.execute(select(observed_state).where(observed_state.c.integration_id == row["id"],
                                                      observed_state.c.external_id == "host")).mappings().first()
    ref = conn.execute(select(external_ref).where(external_ref.c.integration_id == row["id"],
                                                  external_ref.c.external_id == "host")).mappings().first()
    return render(request, "agent.html", {"row": row, "state": state, "ref": ref})


@router.post("/admin/agenci/{integration_id:int}/usun")
def agent_delete(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            row = integrations.get(conn, integration_id)
        except integrations.IntegrationError as e:
            raise NotFound(str(e)) from None
        if row["type"] != agents.TYPE:
            raise NotFound("To nie jest agent.")
        integrations.remove(conn, integration_id)
        auth.audit(conn, "agent_delete", user=sess["login"], ip=client_ip(request), target=row["name"])
    return RedirectResponse("/admin/integracje", 303)
