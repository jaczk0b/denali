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
def _rack_page(request, conn, error=None, message=None, status_code=200):
    placed = {r.device_id: r.name for r in conn.execute(
        select(rack_item.c.device_id, rack.c.name).join(rack, rack.c.id == rack_item.c.rack_id)
        .where(rack_item.c.device_id.isnot(None)))}
    ctx = {"tabs": views.tabs(conn), "racks": racks.overview(conn), "marks": status.device_marks(conn),
           "states": live.saved(conn), "placed": placed, "error": error, "message": message,
           "devices": conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()}
    return render(request, "infra_szafa.html", ctx, status_code)


@router.get("/infrastruktura/szafa")
def tab_rack(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return _rack_page(request, conn)


def _rack_change(request, sess, fn, action, target=""):
    with request.app.state.engine.begin() as conn:
        try:
            fn(conn)
        except racks.RackError as e:
            return _rack_page(request, conn, error=str(e), status_code=400)
        auth.audit(conn, action, user=sess["login"], ip=client_ip(request), target=str(target))
    return RedirectResponse("/infrastruktura/szafa", 303)


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
def rack_place(request: Request, rack_id: int, device_id: str = Form(""), label: str = Form(""), u: str = Form(""),
               height: str = Form("1"), back: str = Form(""), sess: dict = Depends(require_admin)):
    did = int(device_id) if device_id.strip().isdigit() else None
    return _rack_change(request, sess, lambda c: racks.place(c, rack_id, u, height, device_id=did, label=label,
                                                             back=bool(back)), "rack_place", did or label)


@router.post("/infrastruktura/szafa/pozycja/{item_id:int}/usun")
def rack_item_remove(request: Request, item_id: int, sess: dict = Depends(require_admin)):
    return _rack_change(request, sess, lambda c: racks.remove_item(c, item_id), "rack_item_remove", item_id)


# ───────────────────────── mapa ─────────────────────────
@router.get("/infrastruktura/mapa")
def tab_map(request: Request, klienci: str = "", sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        m = mapview.build(conn, live.saved(conn), status.device_marks(conn), with_clients=bool(klienci))
        return render(request, "infra_mapa.html", {"tabs": views.tabs(conn), "m": m, "clients": bool(klienci)})
