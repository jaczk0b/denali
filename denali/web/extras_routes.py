"""Alerty, oś czasu, terminy, własne sprawdzenia i eksport Markdown."""
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select

from .. import alerts, auth, changes, checks, export_md, inventory as inv, terms
from ..models import device
from ..templating import render
from .deps import NotFound, client_ip, require_admin, require_user

router = APIRouter()


def _back(device_id):
    return RedirectResponse(f"/urzadzenia/{device_id}", 303)


def _device(conn, device_id):
    d = inv.get_device(conn, device_id)
    if not d:
        raise NotFound("Nie ma takiego urządzenia.")
    return d


# ───────────────────────── alerty ─────────────────────────
@router.get("/alerty")
def alert_list(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return render(request, "alerty.html", {"rows": alerts.active(conn), "resolved": alerts.recent(conn),
                                               "silence_days": alerts.SILENCE_DAYS, "now": time.time()})


@router.post("/alerty/{alert_id:int}/wycisz")
def alert_silence(request: Request, alert_id: int, days: int = Form(7), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            alerts.silence(conn, alert_id, days)
        except ValueError:
            pass
        else:
            auth.audit(conn, "alert_silence", user=sess["login"], ip=client_ip(request), target=str(alert_id),
                       params={"dni": days})
    return RedirectResponse("/alerty", 303)


@router.post("/alerty/{alert_id:int}/odcisz")
def alert_unsilence(request: Request, alert_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        alerts.unsilence(conn, alert_id)
        auth.audit(conn, "alert_unsilence", user=sess["login"], ip=client_ip(request), target=str(alert_id))
    return RedirectResponse("/alerty", 303)


# ───────────────────────── oś czasu ─────────────────────────
@router.get("/os-czasu")
def timeline(request: Request, urzadzenie: int | None = None, dni: int = 7, sess: dict = Depends(require_user)):
    dni = dni if dni in (1, 7, 30, 90) else 7
    with request.app.state.engine.connect() as conn:
        rows = changes.timeline(conn, time.time() - dni * 86400, device_id=urzadzenie)
        devices = conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()
    return render(request, "os_czasu.html", {"rows": rows, "devices": devices, "device_id": urzadzenie, "days": dni})


# ───────────────────────── terminy ─────────────────────────
@router.post("/urzadzenia/{device_id:int}/terminy")
def term_add(request: Request, device_id: int, due_on: str = Form(""), label: str = Form(""),
             sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        _device(conn, device_id)
        try:
            terms.add(conn, device_id, due_on, label)
        except terms.TermError as e:
            return render(request, "blad.html", {"title": "Nie zapisano", "text": str(e)}, 400)
        auth.audit(conn, "term_add", user=sess["login"], ip=client_ip(request), target=str(device_id))
    return _back(device_id)


@router.post("/urzadzenia/{device_id:int}/terminy/{term_id:int}/usun")
def term_remove(request: Request, device_id: int, term_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        terms.remove(conn, device_id, term_id)
        auth.audit(conn, "term_remove", user=sess["login"], ip=client_ip(request), target=str(device_id))
    return _back(device_id)


# ───────────────────────── własne sprawdzenia ─────────────────────────
@router.post("/urzadzenia/{device_id:int}/sprawdzenia")
def check_add(request: Request, device_id: int, kind: str = Form(""), target: str = Form(""), expect: str = Form(""),
              sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        _device(conn, device_id)
        try:
            checks.add_custom(conn, device_id, kind, target, expect)
        except checks.CheckError as e:
            return render(request, "blad.html", {"title": "Nie zapisano", "text": str(e)}, 400)
        auth.audit(conn, "check_add", user=sess["login"], ip=client_ip(request), target=str(device_id),
                   params={"kind": kind})
    return _back(device_id)


@router.post("/urzadzenia/{device_id:int}/sprawdzenia/{check_id:int}/usun")
def check_remove(request: Request, device_id: int, check_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        checks.remove_custom(conn, device_id, check_id)
        auth.audit(conn, "check_remove", user=sess["login"], ip=client_ip(request), target=str(device_id))
    return _back(device_id)


# ───────────────────────── eksport Markdown ─────────────────────────
def _markdown(text, name):
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/eksport.md")
def export_all(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        auth.audit(conn, "export_md", user=sess["login"], ip=client_ip(request))
        return _markdown(export_md.overview(conn), "denali.md")


@router.get("/urzadzenia/{device_id:int}/eksport.md")
def export_device(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        _device(conn, device_id)
        auth.audit(conn, "export_md", user=sess["login"], ip=client_ip(request), target=str(device_id))
        return _markdown(export_md.for_device(conn, device_id), f"urzadzenie-{device_id}.md")
