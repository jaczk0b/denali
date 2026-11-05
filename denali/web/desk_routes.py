"""Ikony pulpitu: dodawanie, edycja i kolejność. Wszystko tylko dla administratora."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import auth, desk
from ..models import device
from ..templating import render
from .deps import NotFound, client_ip, require_admin

router = APIRouter()
STEPS = {"gora": -1, "dol": 1}


def _int(v):
    return int(v) if str(v or "").strip().isdecimal() else None


def _audit(request, conn, sess, what, target):
    auth.audit(conn, what, user=sess["login"], ip=client_ip(request), target=str(target))


def _manage(request, conn, selected=None, error=None, status_code=200):
    return render(request, "pulpit_ikony.html", {"board": desk.board(conn), "suggest": desk.suggestions(conn),
                                                 "error": error, "selected": selected}, status_code)


def _tile_form(request, conn, t, error=None, status_code=200):
    devices = conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()
    return render(request, "pulpit_ikona.html", {"t": t, "error": error, "categories": desk.categories(conn),
                                                 "devices": devices, "url_choices": desk.url_choices(conn)}, status_code)


def _chosen_category(conn, category_id, new_name):
    """Nowa nazwa wygrywa z listą; bez wyboru pierwsza kategoria, a gdy nie ma żadnej — domyślna."""
    if (new_name or "").strip():
        return desk.ensure_category(conn, new_name)
    if category_id is not None:
        return category_id
    existing = desk.categories(conn)
    return existing[0]["id"] if existing else desk.ensure_category(conn, desk.DEFAULT_CATEGORY)


@router.get("/pulpit/ikony")
def manage(request: Request, kategoria: str = "", sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _manage(request, conn, _int(kategoria))


@router.post("/pulpit/ikony/propozycja")
def add_suggestion(request: Request, key: str = Form(...), category_id: str = Form(""), wroc: str = Form(""),
                   sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        category = _int(category_id)
        try:
            category = _chosen_category(conn, category, "")
            desk.add_suggestion(conn, key, category)
        except desk.DeskError as e:
            return _manage(request, conn, category, str(e), 400)
        _audit(request, conn, sess, "desk_tile_add", key)
    return RedirectResponse("/" if wroc == "pulpit" else f"/pulpit/ikony?kategoria={category}", 303)


@router.get("/pulpit/ikony/nowa")
def tile_new_form(request: Request, kategoria: str = "", sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _tile_form(request, conn, {"id": None, "category_id": _int(kategoria), "own_name": "", "own_url": "",
                                          "own_icon": "", "device_id": None, "new_category": ""})


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
    return RedirectResponse("/", 303)


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
    return RedirectResponse("/pulpit/ikony", 303)


@router.post("/pulpit/ikony/{tile_id}/usun")
def tile_delete(request: Request, tile_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        desk.delete_tile(conn, tile_id)
        _audit(request, conn, sess, "desk_tile_delete", tile_id)
    return RedirectResponse("/pulpit/ikony", 303)


@router.post("/pulpit/ikony/{tile_id}/przesun")
def tile_move(request: Request, tile_id: int, kierunek: str = Form(...), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        desk.move_tile(conn, tile_id, STEPS.get(kierunek, 0))
    return RedirectResponse("/pulpit/ikony", 303)


@router.post("/pulpit/kategorie")
def category_add(request: Request, name: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            desk.ensure_category(conn, name)
        except desk.DeskError as e:
            return _manage(request, conn, error=str(e), status_code=400)
        _audit(request, conn, sess, "desk_category_add", name)
    return RedirectResponse("/pulpit/ikony", 303)


@router.post("/pulpit/kategorie/{category_id}/nazwa")
def category_rename(request: Request, category_id: int, name: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            desk.rename_category(conn, category_id, name)
        except desk.DeskError as e:
            return _manage(request, conn, error=str(e), status_code=400)
        _audit(request, conn, sess, "desk_category_rename", name)
    return RedirectResponse("/pulpit/ikony", 303)


@router.post("/pulpit/kategorie/{category_id}/przesun")
def category_move(request: Request, category_id: int, kierunek: str = Form(...), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        desk.move_category(conn, category_id, STEPS.get(kierunek, 0))
    return RedirectResponse("/pulpit/ikony", 303)


@router.post("/pulpit/kategorie/{category_id}/usun")
def category_delete(request: Request, category_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        desk.delete_category(conn, category_id)
        _audit(request, conn, sess, "desk_category_delete", category_id)
    return RedirectResponse("/pulpit/ikony", 303)
