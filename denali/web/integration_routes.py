"""Integracje w panelu (tylko administrator): dodanie, sprawdzenie połączenia, odczyt na żądanie, wyłączenie."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from .. import auth, integrations
from ..adapters import AdapterError, registry
from ..models import external_ref, integration, observed_state
from ..templating import render
from .deps import NotFound, client_ip, require_admin

router = APIRouter()
log = logging.getLogger("denali.integrations")
TEST_TIMEOUT = 30


def _transport(request):
    return getattr(request.app.state, "adapter_transport", None)   # w testach: nagrane odpowiedzi API


def _list_page(request, conn):
    rows = conn.execute(select(integration).order_by(integration.c.name)).mappings().all()
    stats = {}
    for r in rows:
        states = dict(conn.execute(select(observed_state.c.state, func.count()).where(
            observed_state.c.integration_id == r["id"]).group_by(observed_state.c.state)).all())
        pending = conn.execute(select(func.count()).select_from(external_ref).where(
            external_ref.c.integration_id == r["id"], external_ref.c.status == "nowy")).scalar()
        stats[r["id"]] = {"states": states, "pending": pending}
    return render(request, "integracje.html", {"rows": rows, "stats": stats, "types": registry()})


@router.get("/admin/integracje")
def integration_list(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _list_page(request, conn)


def _form(request, cls, row=None, values=None, error=None, message=None, objects=(), status_code=200):
    return render(request, "integracja.html", {"cls": cls, "row": row, "values": values or {}, "error": error,
                                               "message": message, "objects": objects}, status_code)


def _objects(conn, integration_id):
    return conn.execute(
        select(observed_state, external_ref.c.status.label("ref_status"), external_ref.c.device_id,
               external_ref.c.proposed)
        .join(external_ref, (external_ref.c.integration_id == observed_state.c.integration_id)
              & (external_ref.c.external_id == observed_state.c.external_id), isouter=True)
        .where(observed_state.c.integration_id == integration_id).order_by(observed_state.c.external_id)
    ).mappings().all()


@router.get("/admin/integracje/nowa")
def integration_new(request: Request, typ: str = "proxmox", sess: dict = Depends(require_admin)):
    try:
        cls = integrations.adapter_class(typ)
    except integrations.IntegrationError:
        raise NotFound("Nie ma takiego rodzaju integracji.") from None
    values = {f.key: f.default for f in cls.fields}
    values.update(name=cls.label, poll_seconds=cls.default_poll)
    return _form(request, cls, values=values)


async def _test(request, row):
    try:
        adapter = integrations.adapter_for(row, request.app.state.box, _transport(request))
        return await asyncio.wait_for(adapter.test(), TEST_TIMEOUT), None
    except AdapterError as e:
        return None, str(e)
    except asyncio.TimeoutError:
        return None, f"Brak odpowiedzi w ciągu {TEST_TIMEOUT} s."
    except Exception as e:      # nieprzewidziany błąd adaptera: komunikat zamiast „Internal Server Error”
        log.exception("Sprawdzenie integracji %s nie powiodło się", row["name"])
        return None, f"Nieoczekiwany błąd ({e.__class__.__name__}: {str(e)[:200]}). Szczegóły w logu kontenera app."


@router.post("/admin/integracje/nowa")
async def integration_create(request: Request, sess: dict = Depends(require_admin)):
    form = await request.form()
    try:
        cls = integrations.adapter_class(form.get("type", ""))
    except integrations.IntegrationError as e:
        raise NotFound(str(e)) from None
    config, secrets = integrations.split_form(cls, form)
    with request.app.state.engine.begin() as conn:
        try:
            iid = integrations.save(conn, request.app.state.box, cls.type, form.get("name", ""), config, secrets,
                                    form.get("poll_seconds"))
        except (integrations.IntegrationError, ValueError) as e:
            return _form(request, cls, values={**config, "name": form.get("name", ""),
                                                "poll_seconds": form.get("poll_seconds")}, error=str(e), status_code=400)
        auth.audit(conn, "integration_create", user=sess["login"], ip=client_ip(request), target=form.get("name", ""),
                   params={"type": cls.type})
    return RedirectResponse(f"/admin/integracje/{iid}?sprawdz=1", 303)


@router.get("/admin/integracje/{integration_id:int}")
async def integration_view(request: Request, integration_id: int, sprawdz: str = "", sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        try:
            row = integrations.get(conn, integration_id)
        except integrations.IntegrationError as e:
            raise NotFound(str(e)) from None
        objects = _objects(conn, integration_id)
    message = error = None
    if sprawdz:
        message, error = await _test(request, row)
    cls = integrations.adapter_class(row["type"])
    return _form(request, cls, row=row, values={**(row["config"] or {}), "name": row["name"],
                                                 "poll_seconds": row["poll_seconds"]},
                 message=message, error=error, objects=objects)


@router.post("/admin/integracje/{integration_id:int}")
async def integration_update(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    form = await request.form()
    box = request.app.state.box
    with request.app.state.engine.begin() as conn:
        try:
            row = integrations.get(conn, integration_id)
        except integrations.IntegrationError as e:
            raise NotFound(str(e)) from None
        cls = integrations.adapter_class(row["type"])
        config, secrets = integrations.split_form(cls, form, integrations.secrets_of(row, box))
        try:
            integrations.save(conn, box, row["type"], form.get("name", ""), config, secrets, form.get("poll_seconds"),
                              integration_id=integration_id)
        except (integrations.IntegrationError, ValueError) as e:
            return _form(request, cls, row=row, values={**config, "name": form.get("name", ""),
                                                         "poll_seconds": form.get("poll_seconds")},
                         error=str(e), objects=_objects(conn, integration_id), status_code=400)
        auth.audit(conn, "integration_update", user=sess["login"], ip=client_ip(request), target=row["name"])
    return RedirectResponse(f"/admin/integracje/{integration_id}?sprawdz=1", 303)


@router.post("/admin/integracje/{integration_id:int}/odczyt")
async def integration_poll(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        try:
            row = integrations.get(conn, integration_id)
        except integrations.IntegrationError as e:
            raise NotFound(str(e)) from None
    error = await integrations.poll_one(request.app.state.engine, request.app.state.box, row, _transport(request))
    with request.app.state.engine.connect() as conn:
        return _form(request, integrations.adapter_class(row["type"]), row=integrations.get(conn, integration_id),
                     values={**(row["config"] or {}), "name": row["name"], "poll_seconds": row["poll_seconds"]},
                     message=None if error else "Odczytano. Nowe obiekty czekają w „Do zatwierdzenia”.",
                     error=error, objects=_objects(conn, integration_id), status_code=200 if not error else 502)


@router.post("/admin/integracje/{integration_id:int}/wlacz")
def integration_enable(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    return _toggle(request, integration_id, True, sess)


@router.post("/admin/integracje/{integration_id:int}/wylacz")
def integration_disable(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    return _toggle(request, integration_id, False, sess)


def _get_or_404(conn, integration_id):
    try:
        return integrations.get(conn, integration_id)
    except integrations.IntegrationError as e:
        raise NotFound(str(e)) from None


def _toggle(request, integration_id, enabled, sess):
    with request.app.state.engine.begin() as conn:
        row = _get_or_404(conn, integration_id)
        integrations.set_enabled(conn, integration_id, enabled)
        auth.audit(conn, "integration_enable" if enabled else "integration_disable", user=sess["login"],
                   ip=client_ip(request), target=row["name"])
    return RedirectResponse("/admin/integracje", 303)


@router.post("/admin/integracje/{integration_id:int}/usun")
def integration_delete(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        row = _get_or_404(conn, integration_id)
        integrations.remove(conn, integration_id)
        auth.audit(conn, "integration_delete", user=sess["login"], ip=client_ip(request), target=row["name"])
    return RedirectResponse("/admin/integracje", 303)
