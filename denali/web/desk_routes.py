"""Edycja pulpitu: tryb edycji na samym pulpicie (/?edycja=1) i zapisy, które z niego idą. Tylko dla administratora."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from .. import auth, desk
from ..models import device
from ..templating import render
from .deps import NotFound, client_ip, require_admin

router = APIRouter()
EDIT_URL = "/?edycja=1"


def _int(v):
    return int(v) if str(v or "").strip().isdecimal() else None


def _audit(request, conn, sess, what, target):
    auth.audit(conn, what, user=sess["login"], ip=client_ip(request), target=str(target))


def _tile_form(request, conn, t, error=None, status_code=200):
    devices = conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()
    return render(request, "pulpit_ikona.html", {"t": t, "error": error, "categories": desk.categories(conn),
                                                 "devices": devices, "url_choices": desk.url_choices(conn)}, status_code)


def _empty_tile(category_id=None):
    return {"id": None, "category_id": category_id, "own_name": "", "own_url": "", "own_icon": "", "device_id": None, "new_category": ""}


def _chosen_category(conn, category_id, new_name):
    """Nowa nazwa wygrywa z listą; bez wyboru pierwsza kategoria, a gdy nie ma żadnej — domyślna."""
    if (new_name or "").strip():
        return desk.ensure_category(conn, new_name)
    if category_id is not None:
        return category_id
    existing = desk.categories(conn)
    return existing[0]["id"] if existing else desk.ensure_category(conn, desk.DEFAULT_CATEGORY)


@router.get("/pulpit/ikony")
def manage(sess: dict = Depends(require_admin)):
    return RedirectResponse(EDIT_URL, 303)


@router.post("/pulpit/ikony/propozycja")
def add_suggestion(request: Request, key: str = Form(...), category_id: str = Form(""), wroc: str = Form(""),
                   sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        category = _int(category_id)
        try:
            category = _chosen_category(conn, category, "")
            desk.add_suggestion(conn, key, category)
        except desk.DeskError as e:
            return _tile_form(request, conn, _empty_tile(category), str(e), 400)
        _audit(request, conn, sess, "desk_tile_add", key)
    return RedirectResponse("/" if wroc == "pulpit" else f"{EDIT_URL}&dodaj={category}", 303)


@router.get("/pulpit/ikony/nowa")
def tile_new_form(request: Request, kategoria: str = "", sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _tile_form(request, conn, _empty_tile(_int(kategoria)))


def _tile_fields(form):
    return {"category_id": _int(form.get("category_id")), "own_name": form.get("name", ""),
            "own_url": form.get("url", ""), "own_icon": form.get("icon", ""),
            "device_id": _int(form.get("device_id")), "new_category": form.get("new_category", "")}


@router.post("/pulpit/ikony/nowa")
async def tile_create(request: Request, sess: dict = Depends(require_admin)):
    t = _tile_fields(await request.form())
    with request.app.state.engine.begin() as conn:
        try:
            desk.add_tile(conn, _chosen_category(conn, t["category_id"], t["new_category"]), t["own_name"],
                          t["own_url"], t["own_icon"], t["device_id"])
        except desk.DeskError as e:
            return _tile_form(request, conn, {"id": None, **t}, str(e), 400)
        _audit(request, conn, sess, "desk_tile_add", t["own_name"] or f"urządzenie {t['device_id']}")
    return RedirectResponse(EDIT_URL, 303)


@router.get("/pulpit/ikony/{tile_id}/edycja")
def tile_edit_form(request: Request, tile_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        t = desk.get_tile(conn, tile_id)
        if not t:
            raise NotFound("Nie ma takiej ikony.")
        return _tile_form(request, conn, {**t, "new_category": ""})


@router.post("/pulpit/ikony/{tile_id}/edycja")
async def tile_update(request: Request, tile_id: int, sess: dict = Depends(require_admin)):
    t = _tile_fields(await request.form())
    with request.app.state.engine.begin() as conn:
        if not desk.get_tile(conn, tile_id):
            raise NotFound("Nie ma takiej ikony.")
        try:
            desk.update_tile(conn, tile_id, _chosen_category(conn, t["category_id"], t["new_category"]), t["own_name"],
                             t["own_url"], t["own_icon"], t["device_id"])
        except desk.DeskError as e:
            return _tile_form(request, conn, {"id": tile_id, **t}, str(e), 400)
        _audit(request, conn, sess, "desk_tile_update", tile_id)
    return RedirectResponse(EDIT_URL, 303)


@router.post("/pulpit/kategorie")
def category_add(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        new_id = desk.new_category(conn)
        _audit(request, conn, sess, "desk_category_add", new_id)
    return RedirectResponse(f"{EDIT_URL}#nowa-{new_id}", 303)


# ───────────────────────── zapisy z trybu edycji (JSON, token CSRF w nagłówku) ─────────────────────────
def _failed(e):
    return JSONResponse({"blad": str(e)}, 400)


async def _body(request):
    try:
        data = await request.json()
    except ValueError:
        raise desk.DeskError("Zły format danych.") from None
    if not isinstance(data, dict):
        raise desk.DeskError("Zły format danych.")
    return data


@router.post("/pulpit/api/uklad")
async def api_layout(request: Request, sess: dict = Depends(require_admin)):
    try:
        data = await _body(request)
        with request.app.state.engine.begin() as conn:
            desk.save_layout(conn, data)
            _audit(request, conn, sess, "desk_layout", ",".join(sorted(data)))
    except desk.DeskError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/pulpit/api/nazwa")
async def api_rename(request: Request, sess: dict = Depends(require_admin)):
    try:
        data = await _body(request)
        category = data.get("id")
        with request.app.state.engine.begin() as conn:
            desk.rename_category(conn, category, data.get("name"))
            _audit(request, conn, sess, "desk_category_rename", data.get("name"))
    except desk.DeskError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/pulpit/api/usun")
async def api_delete(request: Request, sess: dict = Depends(require_admin)):
    try:
        data = await _body(request)
        with request.app.state.engine.begin() as conn:
            token, name = desk.remove(conn, data.get("kind"), data.get("id"))
            _audit(request, conn, sess, f"desk_delete_{data.get('kind')}", name)
    except desk.DeskError as e:
        return _failed(e)
    return {"token": token, "name": name}


@router.post("/pulpit/api/cofnij")
async def api_restore(request: Request, sess: dict = Depends(require_admin)):
    try:
        data = await _body(request)
        with request.app.state.engine.begin() as conn:
            desk.restore(conn, str(data.get("token")))
            _audit(request, conn, sess, "desk_restore", "")
    except desk.DeskError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/pulpit/api/widzet")
async def api_widget(request: Request, sess: dict = Depends(require_admin)):
    try:
        data = await _body(request)
        with request.app.state.engine.begin() as conn:
            desk.resize_widget(conn, data.get("id"), data.get("size"))
    except desk.DeskError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/pulpit/api/wyglad")
async def api_look(request: Request, sess: dict = Depends(require_admin)):
    try:
        data = await _body(request)
        with request.app.state.engine.begin() as conn:
            desk.save_look(conn, data)
    except desk.DeskError as e:
        return _failed(e)
    return {"ok": True}
