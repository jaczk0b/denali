"""Strony konta z rolą „uzytkownik” (/moje) i ich obsługa przez administratora (/admin/udostepnianie)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import auth, shares
from ..models import device
from ..templating import render
from .deps import NotFound, client_ip, require_admin, require_fresh, require_user

router = APIRouter()
ADMIN_PAGE = "/admin/udostepnianie"


def _int(v):
    return int(v) if str(v or "").strip().isdecimal() else None


def _audit(request, conn, sess, what, target):
    auth.audit(conn, what, user=sess["login"], ip=client_ip(request), target=str(target))


# ───────────────────────── widok użytkownika ─────────────────────────
@router.get("/moje")
def mine(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        items = shares.with_status(conn, shares.for_session(conn, sess))
    return render(request, "moje.html", {"items": [i for i in items if i["kind"] != "status"]})


@router.get("/moje/status")
def mine_status(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        items = shares.with_status(conn, shares.for_session(conn, sess))
    shown = [i for i in items if i["status"]]
    return render(request, "moje_status.html", {"items": shown, "summary": shares.summary(shown)})


@router.get("/moje/{share_id:int}")
def mine_guide(request: Request, share_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        s = next((r for r in shares.for_session(conn, sess) if r["id"] == share_id), None)
    if not s or not s["body_md"]:
        raise NotFound()
    return render(request, "moje_instrukcja.html", {"s": s})


# ───────────────────────── administrator ─────────────────────────
def _admin_page(request, conn, error=None, revealed=None, status_code=200):
    return render(request, "admin_udostepnianie.html", {
        "accounts": shares.accounts(conn), "shares": shares.all_shares(conn), "share_kinds": shares.KINDS,
        "error": error, "revealed": revealed}, status_code)


def _share_form(request, conn, s, error=None, status_code=200):
    devices = conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()
    return render(request, "admin_udostepnienie.html", {"s": s, "error": error, "devices": devices,
                                                        "share_kinds": shares.KINDS}, status_code)


@router.get(ADMIN_PAGE)
def admin_page(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _admin_page(request, conn)


@router.post(f"{ADMIN_PAGE}/konta")
async def account_create(request: Request, login: str = Form(""), sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)
    with request.app.state.engine.begin() as conn:
        try:
            _, password = shares.create_account(conn, login)
        except auth.AuthError as e:
            return _admin_page(request, conn, error=str(e), status_code=400)
        _audit(request, conn, sess, "account_create", login)
        return _admin_page(request, conn, revealed={"login": login.strip(), "password": password})


def _account(conn, user_id):
    u = shares.get_account(conn, user_id)
    if not u:
        raise NotFound("Nie ma takiego konta.")
    return u


@router.post(f"{ADMIN_PAGE}/konta/{{user_id}}/uslugi")
async def account_shares(request: Request, user_id: int, sess: dict = Depends(require_admin)):
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        u = _account(conn, user_id)
        shares.assign(conn, user_id, [i for i in map(_int, form.getlist("share")) if i is not None])
        _audit(request, conn, sess, "account_shares", u["login"])
    return RedirectResponse(ADMIN_PAGE, 303)


@router.post(f"{ADMIN_PAGE}/konta/{{user_id}}/haslo")
async def account_password(request: Request, user_id: int, sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)
    with request.app.state.engine.begin() as conn:
        u = _account(conn, user_id)
        password = shares.new_password()
        auth.set_password(conn, user_id, password)
        _audit(request, conn, sess, "account_password_reset", u["login"])
        return _admin_page(request, conn, revealed={"login": u["login"], "password": password})


@router.post(f"{ADMIN_PAGE}/konta/{{user_id}}/wlacz")
def account_toggle(request: Request, user_id: int, wylacz: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        u = _account(conn, user_id)
        shares.set_disabled(conn, user_id, wylacz == "1")
        _audit(request, conn, sess, "account_disable" if wylacz == "1" else "account_enable", u["login"])
    return RedirectResponse(ADMIN_PAGE, 303)


def _share_fields(form):
    return {"title": form.get("title", ""), "kind": form.get("kind", ""), "target": form.get("target", ""),
            "device_id": _int(form.get("device_id")), "body_md": form.get("body_md", "")}


@router.get(f"{ADMIN_PAGE}/uslugi/nowa")
def share_new(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _share_form(request, conn, {"id": None, "title": "", "kind": "link", "target": "", "device_id": None,
                                           "body_md": ""})


@router.post(f"{ADMIN_PAGE}/uslugi/nowa")
async def share_create(request: Request, sess: dict = Depends(require_admin)):
    s = _share_fields(await request.form())
    with request.app.state.engine.begin() as conn:
        try:
            shares.create(conn, **s)
        except shares.ShareError as e:
            return _share_form(request, conn, {"id": None, **s}, str(e), 400)
        _audit(request, conn, sess, "share_create", s["title"])
    return RedirectResponse(ADMIN_PAGE, 303)


@router.get(f"{ADMIN_PAGE}/uslugi/{{share_id}}")
def share_edit_form(request: Request, share_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        s = shares.get(conn, share_id)
        if not s:
            raise NotFound()
        return _share_form(request, conn, s)


@router.post(f"{ADMIN_PAGE}/uslugi/{{share_id}}")
async def share_update(request: Request, share_id: int, sess: dict = Depends(require_admin)):
    s = _share_fields(await request.form())
    with request.app.state.engine.begin() as conn:
        if not shares.get(conn, share_id):
            raise NotFound()
        try:
            shares.edit(conn, share_id, **s)
        except shares.ShareError as e:
            return _share_form(request, conn, {"id": share_id, **s}, str(e), 400)
        _audit(request, conn, sess, "share_update", s["title"])
    return RedirectResponse(ADMIN_PAGE, 303)


@router.post(f"{ADMIN_PAGE}/uslugi/{{share_id}}/usun")
def share_delete(request: Request, share_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        shares.remove(conn, share_id)
        _audit(request, conn, sess, "share_delete", share_id)
    return RedirectResponse(ADMIN_PAGE, 303)
