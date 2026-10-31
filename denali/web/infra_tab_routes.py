"""Zakładki działu Infrastruktura. Odczyt: każdy zalogowany. Szafa (dokumentacja) — zmiany tylko administrator."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import auth, infra_views as views, live, mapview, racks, status
from ..models import device, rack, rack_item
from ..templating import render
from .deps import client_ip, require_admin, require_user

router = APIRouter()


def _page(request, template, key, build):
    with request.app.state.engine.connect() as conn:
        tabs = views.tabs(conn)
        if key not in {k for k, _l, _u in tabs}:
            return RedirectResponse("/urzadzenia", 303)     # źródło nie jest podłączone — nie ma czego pokazać
        ctx = build(conn)
    return render(request, template, {"tabs": tabs, **ctx})


@router.get("/infrastruktura")
def infra_home(sess: dict = Depends(require_user)):
    return RedirectResponse("/infrastruktura/mapa", 303)


@router.get("/infrastruktura/proxmox")
def tab_proxmox(request: Request, sess: dict = Depends(require_user)):
    return _page(request, "infra_proxmox.html", "proxmox", lambda c: {"clusters": views.proxmox(c)})


@router.get("/infrastruktura/truenas")
def tab_truenas(request: Request, sess: dict = Depends(require_user)):
    return _page(request, "infra_truenas.html", "truenas", lambda c: {"nases": views.truenas(c)})


@router.get("/infrastruktura/docker")
def tab_docker(request: Request, sess: dict = Depends(require_user)):
    return _page(request, "infra_docker.html", "docker", lambda c: {"komodos": views.docker(c)})


@router.get("/infrastruktura/siec")
def tab_network(request: Request, sess: dict = Depends(require_user)):
    def build(conn):
        nets = views.network(conn)
        return {"nets": nets, "clients": views.all_clients(nets), "weak_signal": views.WEAK_SIGNAL}
    return _page(request, "infra_siec.html", "siec", build)


@router.get("/infrastruktura/adresy")
def tab_addresses(request: Request, sess: dict = Depends(require_user)):
    return _page(request, "infra_adresy.html", "adresy", lambda c: {"nets": views.addresses(c)})


# ───────────────────────── szafa ─────────────────────────
# Edycja kliknięciami: wolne U → panel „Dodaj w U…”, pozycja → panel zmian. Zaznaczenie w adresie (?szafa=&u=&tyl=
# albo ?pozycja=), więc działa bez JavaScriptu, a po zapisie wraca się do tego samego miejsca.
HEIGHTS = (1, 2, 3, 4, 6, 8)
WHAT = {"urzadzenie": "Urządzenie", "polka": "Półka", "patch": "Patch panel", "opis": "Opis / zaślepka"}


def _int_or_none(v):
    return int(v) if str(v or "").strip().isdigit() else None


def _rack_page(request, conn, sel=None, error=None, status_code=200):
    sel = sel or {}
    placed = {r.device_id: r.name for r in conn.execute(
        select(rack_item.c.device_id, rack.c.name).join(rack, rack.c.id == rack_item.c.rack_id)
        .where(rack_item.c.device_id.isnot(None)))}
    all_racks = racks.overview(conn)
    item = None
    if sel.get("pozycja"):
        try:
            item = racks.get_item(conn, sel["pozycja"])
        except racks.RackError:
            item = None
    if item and item["shelf_id"]:
        item = racks.get_item(conn, item["shelf_id"])          # klik w rzecz na półce = panel półki
    for x in all_racks:
        x["sel_item"] = item if item and item["rack_id"] == x["rack"]["id"] else None
        x["sel_u"] = sel.get("u") if sel.get("szafa") == x["rack"]["id"] and not item else None
        x["sel_back"] = bool(sel.get("tyl")) and sel.get("szafa") == x["rack"]["id"]
        if x["sel_item"]:
            x["sel_item"]["on_shelf"] = [e for e in x["entries"] if e["shelf_id"] == item["id"]]
            rows = x["back"] if item["back"] else x["front"]
            empty = {r["top"] for r in rows if not r["item"]}
            above = racks.free_above(rows, item["u"] + item["height"])
            x["max_h"] = item["height"] + above                   # można urosnąć w górę o wolne U
            x["can_up"], x["can_down"] = above > 0, item["u"] - 1 in empty
        if x["sel_u"]:
            x["max_h"] = racks.free_above(x["back"] if x["sel_back"] else x["front"], x["sel_u"])
    ctx = {"tabs": views.tabs(conn), "racks": all_racks, "marks": status.device_marks(conn),
           "states": live.saved(conn), "placed": placed, "error": error, "heights": HEIGHTS, "what": WHAT,
           "devices": conn.execute(select(device.c.id, device.c.name, device.c.kind).order_by(device.c.name)
                                   ).mappings().all()}
    return render(request, "infra_szafa.html", ctx, status_code)


@router.get("/infrastruktura/szafa")
def tab_rack(request: Request, szafa: str = "", u: str = "", tyl: str = "", pozycja: str = "",
             sess: dict = Depends(require_user)):
    sel = {"szafa": _int_or_none(szafa), "u": _int_or_none(u), "tyl": tyl == "1", "pozycja": _int_or_none(pozycja)}
    with request.app.state.engine.connect() as conn:
        return _rack_page(request, conn, sel if sess["role"] == "admin" else None)


def _rack_change(request, sess, fn, action, target="", sel=None):
    """fn zwraca (opcjonalnie) adres, do którego wrócić — np. panel nowo dodanej pozycji."""
    with request.app.state.engine.begin() as conn:
        try:
            back_to = fn(conn)
        except racks.RackError as e:
            return _rack_page(request, conn, sel, error=str(e), status_code=400)
        auth.audit(conn, action, user=sess["login"], ip=client_ip(request), target=str(target))
    return RedirectResponse(back_to if isinstance(back_to, str) else "/infrastruktura/szafa", 303)


def _item_url(item_id):
    return f"/infrastruktura/szafa?pozycja={item_id}"


@router.post("/infrastruktura/szafa")
def rack_add(request: Request, name: str = Form(""), units: str = Form("42"), place: str = Form(""),
             sess: dict = Depends(require_admin)):
    return _rack_change(request, sess, lambda c: racks.save_rack(c, name, units, place), "rack_add", name)


@router.post("/infrastruktura/szafa/{rack_id:int}")
def rack_edit(request: Request, rack_id: int, name: str = Form(""), units: str = Form("42"), place: str = Form(""),
              notes: str = Form(""), sess: dict = Depends(require_admin)):
    return _rack_change(request, sess, lambda c: racks.save_rack(c, name, units, place, notes, rack_id=rack_id),
                        "rack_edit", rack_id)


@router.post("/infrastruktura/szafa/{rack_id:int}/usun")
def rack_delete(request: Request, rack_id: int, sess: dict = Depends(require_admin)):
    return _rack_change(request, sess, lambda c: racks.delete_rack(c, rack_id), "rack_delete", rack_id)


@router.post("/infrastruktura/szafa/{rack_id:int}/umiesc")
def rack_place(request: Request, rack_id: int, what: str = Form("urzadzenie"), device_id: str = Form(""),
               label: str = Form(""), u: str = Form(""), height: str = Form("1"), back: str = Form(""),
               shelf_id: str = Form(""), sess: dict = Depends(require_admin)):
    did, sid = _int_or_none(device_id), _int_or_none(shelf_id)
    label = label.strip()
    if what == "polka":
        did, label = None, label or "półka"
    elif what == "patch":
        did, label = None, label or "patch panel"
    elif what == "opis":
        did = None
    elif did is None:
        sel = {"szafa": rack_id, "u": _int_or_none(u), "tyl": bool(back)}
        with request.app.state.engine.connect() as conn:
            return _rack_page(request, conn, sel, error="Wybierz urządzenie z listy.", status_code=400)

    def go(c):
        new = racks.place(c, rack_id, u, height, device_id=did, label=label, back=bool(back),
                          shelf=what == "polka", shelf_id=sid)
        return _item_url(sid or new)
    sel = {"szafa": rack_id, "u": _int_or_none(u), "tyl": bool(back), "pozycja": sid}
    return _rack_change(request, sess, go, "rack_place", did or label, sel)


@router.post("/infrastruktura/szafa/pozycja/{item_id:int}")
def rack_item_change(request: Request, item_id: int, u: str = Form(""), height: str = Form(""),
                     back: str = Form(""), label: str = Form(None), sess: dict = Depends(require_admin)):
    def go(c):
        racks.change(c, item_id, u=_int_or_none(u), height=_int_or_none(height),
                     back=None if back == "" else back == "1", label=label)
        return _item_url(item_id)
    return _rack_change(request, sess, go, "rack_item_change", item_id, {"pozycja": item_id})


@router.post("/infrastruktura/szafa/pozycja/{item_id:int}/usun")
def rack_item_remove(request: Request, item_id: int, wroc: str = Form(""), sess: dict = Depends(require_admin)):
    def go(c):
        racks.remove_item(c, item_id)
        return _item_url(int(wroc)) if wroc.isdigit() else None
    return _rack_change(request, sess, go, "rack_item_remove", item_id)


# ───────────────────────── mapa ─────────────────────────
@router.get("/infrastruktura/mapa")
def tab_map(request: Request, klienci: str = "", sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        m = mapview.build(conn, live.saved(conn), status.device_marks(conn), with_clients=bool(klienci))
        return render(request, "infra_mapa.html", {"tabs": views.tabs(conn), "m": m, "clients": bool(klienci)})
