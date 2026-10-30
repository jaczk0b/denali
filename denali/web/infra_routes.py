"""Pulpit, infrastruktura (lista, karta urządzenia, edycja), kolejka „do zatwierdzenia”, wyszukiwarka.

Odczyt: każdy zalogowany. Zmiany: tylko administrator (require_admin sprawdza też CSRF).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, or_, select

from .. import auth, discovery, docs, files, incidents, integrations, inventory as inv, profiles, status, vault
from ..models import (cable, dependency, device, device_address, external_ref, incident, integration, procedure,
                      procedure_step, runbook)
from ..templating import render
from .deps import NotFound, Redirect, client_ip, require_admin, require_user

router = APIRouter()
EDIT_SETTINGS = ("monitor", "down_after_s", "notify", "updates")   # akcje dojdą w Etapie 5
DAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]


def _back(url):
    return RedirectResponse(url, 303)


def _int(v):
    try:
        return int(v) if v is not None and str(v).strip() else None
    except (TypeError, ValueError):
        return None


# ───────────────────────── pulpit ─────────────────────────
@router.get("/")
def pulpit(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        devices = conn.execute(select(device.c.id, device.c.name, device.c.kind)).mappings().all()
        marks = status.device_marks(conn)
        open_inc = status.open_incidents(conn)
        effects = {i["id"]: conn.execute(
            select(func.count()).select_from(incidents.incident_device)
            .where(incidents.incident_device.c.incident_id == i["id"],
                   incidents.incident_device.c.role.in_(("skutek", "gorzej")))).scalar() for i in open_inc}
        ctx = {
            "apps": status.pulpit_apps(conn), "marks": marks, "open_incidents": open_inc, "effects": effects,
            "layers": status.layers(devices, marks), "recent": status.recent_events(conn),
            "no_devices": not devices,
        }
    now = datetime.now()
    ctx.update(clock=now.strftime("%H:%M"), date=f"{DAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}")
    return render(request, "pulpit.html", ctx)


# ───────────────────────── lista i karta ─────────────────────────
@router.get("/urzadzenia")
def device_list(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(select(device).order_by(device.c.name)).mappings().all()
        marks = status.device_marks(conn)
        ips = {r.device_id: r.value for r in conn.execute(
            select(device_address.c.device_id, device_address.c.value)
            .where(device_address.c.kind == "ip").order_by(device_address.c.is_primary))}
        names = {r["id"]: r["name"] for r in rows}
    groups = []
    for key, label, _k in status.LAYERS + [("inne", "Inne", set())]:
        devs = [d for d in rows if status.layer_of(d["kind"]) == key]
        if devs:
            groups.append((label, devs))
    return render(request, "urzadzenia.html", {"groups": groups, "marks": marks, "ips": ips, "names": names})


def _card(request, conn, device_id, *, error=None, message=None, revealed=None, status_code=200):
    d = inv.get_device(conn, device_id)
    if not d:
        raise NotFound("Nie ma takiego urządzenia.")
    g = inv.load_graph(conn)
    hard, soft = g.impact(device_id)
    all_devices = conn.execute(select(device.c.id, device.c.name, device.c.kind).order_by(device.c.name)
                               ).mappings().all()
    profile_name, settings = profiles.effective(conn, device_id)
    is_admin = request.state.sess["role"] == "admin"
    ctx = {
        "d": d, "error": error, "message": message, "revealed": revealed,
        "parent": inv.get_device(conn, d["parent_id"]) if d["parent_id"] else None,
        "children": conn.execute(select(device.c.id, device.c.name, device.c.kind)
                                 .where(device.c.parent_id == device_id).order_by(device.c.name)).mappings().all(),
        "addresses": conn.execute(select(device_address).where(device_address.c.device_id == device_id)
                                  .order_by(device_address.c.kind, device_address.c.value)).mappings().all(),
        "deps_up": conn.execute(select(dependency, device.c.name).join(device, device.c.id == dependency.c.depends_on_id)
                                .where(dependency.c.device_id == device_id)).mappings().all(),
        "deps_down": conn.execute(select(dependency, device.c.name).join(device, device.c.id == dependency.c.device_id)
                                  .where(dependency.c.depends_on_id == device_id)).mappings().all(),
        "cables": inv.device_cables(conn, device_id),
        "impact_hard": sorted(g.name(x) for x in hard), "impact_soft": sorted(g.name(x) for x in soft),
        "names": {x["id"]: x["name"] for x in all_devices}, "all_devices": all_devices,
        "profile_name": profile_name, "settings": settings, "setting_defs": profiles.SETTINGS,
        "edit_settings": EDIT_SETTINGS,
        "procedures": docs.procedures_for_device(conn, device_id),
        "runbooks": docs.runbooks_for_device(conn, device_id),
        "incidents": incidents.list_incidents(conn, device_id=device_id, limit=20),
        "files": files.for_device(conn, device_id),
        "secrets": vault.list_secrets(conn, role="admin", device_id=device_id) if is_admin else [],
        "marks": (marks := status.device_marks(conn)), "mark": marks.get(device_id),
        "sources": integrations.states_for_device(conn, device_id),
        "refs": conn.execute(select(external_ref).where(external_ref.c.device_id == device_id)).mappings().all(),
    }
    return render(request, "urzadzenie.html", ctx, status_code)


@router.get("/urzadzenia/{device_id:int}")
def device_card(request: Request, device_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return _card(request, conn, device_id)


def _edit_form(request, conn, d, error=None, status_code=200):
    all_devices = conn.execute(select(device.c.id, device.c.name, device.c.kind).order_by(device.c.name)
                               ).mappings().all()
    profs = conn.execute(select(profiles.profile.c.id, profiles.profile.c.name).order_by(profiles.profile.c.name)
                         ).mappings().all()
    return render(request, "urzadzenie_edycja.html", {"d": d, "error": error, "all_devices": all_devices,
                                                      "profiles": profs, "nest": inv.NEST_IN}, status_code)


@router.get("/urzadzenia/nowe")
def device_new_form(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _edit_form(request, conn, {"id": None, "name": "", "kind": "usluga", "role": "", "criticality": 3,
                                          "parent_id": None, "profile_id": None, "place": "", "serial": "",
                                          "notes_md": "", "icon": "", "app_url": "", "pulpit_group": "brak"})


def _device_fields(form):
    return {
        "name": form.get("name", ""), "kind": form.get("kind", ""), "role": form.get("role", ""),
        "criticality": _int(form.get("criticality")) or 3, "parent_id": _int(form.get("parent_id")),
        "profile_id": _int(form.get("profile_id")), "place": form.get("place", ""),
        "serial": form.get("serial", ""), "notes_md": form.get("notes_md", ""), "icon": form.get("icon", ""),
        "app_url": form.get("app_url", ""), "pulpit_group": form.get("pulpit_group", "brak"),
    }


@router.post("/urzadzenia/nowe")
async def device_create(request: Request, sess: dict = Depends(require_admin)):
    fields = _device_fields(await request.form())
    with request.app.state.engine.begin() as conn:
        try:
            did = inv.create_device(conn, **fields)
        except inv.InventoryError as e:
            return _edit_form(request, conn, {"id": None, **fields}, str(e), 400)
        auth.audit(conn, "device_create", user=sess["login"], ip=client_ip(request), target=fields["name"])
    return _back(f"/urzadzenia/{did}")


@router.get("/urzadzenia/{device_id}/edycja")
def device_edit_form(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        d = inv.get_device(conn, device_id)
        if not d:
            raise NotFound("Nie ma takiego urządzenia.")
        return _edit_form(request, conn, d)


@router.post("/urzadzenia/{device_id}/edycja")
async def device_update(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    fields = _device_fields(await request.form())
    with request.app.state.engine.begin() as conn:
        if not inv.get_device(conn, device_id):
            raise NotFound("Nie ma takiego urządzenia.")
        try:
            inv.update_device(conn, device_id, **fields)
        except inv.InventoryError as e:
            return _edit_form(request, conn, {"id": device_id, **fields}, str(e), 400)
        auth.audit(conn, "device_update", user=sess["login"], ip=client_ip(request), target=fields["name"])
    return _back(f"/urzadzenia/{device_id}")


@router.post("/urzadzenia/{device_id}/usun")
def device_delete(request: Request, device_id: int, confirm: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        d = inv.get_device(conn, device_id)
        if not d:
            raise NotFound("Nie ma takiego urządzenia.")
        if confirm.strip() != d["name"]:
            return _card(request, conn, device_id, error="Żeby usunąć, przepisz dokładnie nazwę urządzenia.",
                         status_code=400)
        inv.delete_device(conn, device_id)
        files.cleanup_orphans(conn, request.app.state.settings.data_dir)
        auth.audit(conn, "device_delete", user=sess["login"], ip=client_ip(request), target=d["name"])
    return _back("/urzadzenia")


# ───────────────────────── adresy, zależności, kable ─────────────────────────
def _card_change(request, device_id, fn, audit_action, sess, target=""):
    """Wspólny schemat zmian na karcie: wykonaj, przy błędzie pokaż kartę z komunikatem, inaczej wróć na kartę."""
    with request.app.state.engine.begin() as conn:
        if not inv.get_device(conn, device_id):
            raise NotFound("Nie ma takiego urządzenia.")
        try:
            fn(conn)
        except (inv.InventoryError, profiles.ProfileError, files.FileError, ValueError) as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
        auth.audit(conn, audit_action, user=sess["login"], ip=client_ip(request), target=target or str(device_id))
    return _back(f"/urzadzenia/{device_id}")


@router.post("/urzadzenia/{device_id}/adresy")
def address_add(request: Request, device_id: int, kind: str = Form(""), value: str = Form(""),
                primary: str = Form(""), sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: inv.add_address(c, device_id, kind, value, primary=bool(primary)),
                        "address_add", sess, value)


@router.post("/urzadzenia/{device_id}/adresy/{address_id}/usun")
def address_remove(request: Request, device_id: int, address_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: c.execute(device_address.delete().where(
        device_address.c.id == address_id, device_address.c.device_id == device_id)), "address_remove", sess)


@router.post("/urzadzenia/{device_id}/zaleznosci")
def dependency_add(request: Request, device_id: int, depends_on_id: str = Form(""), type: str = Form("siec"),
                   hard: str = Form(""), note: str = Form(""), sess: dict = Depends(require_admin)):
    def add(c):
        target = _int(depends_on_id)
        if not target:
            raise inv.InventoryError("Wybierz urządzenie.")
        inv.add_dependency(c, device_id, target, type, hard=bool(hard), note=note)
    return _card_change(request, device_id, add, "dependency_add", sess)


@router.post("/urzadzenia/{device_id}/zaleznosci/{dep_id}/usun")
def dependency_remove(request: Request, device_id: int, dep_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: c.execute(dependency.delete().where(
        dependency.c.id == dep_id, or_(dependency.c.device_id == device_id, dependency.c.depends_on_id == device_id))),
        "dependency_remove", sess)


@router.post("/urzadzenia/{device_id}/kable")
def cable_add(request: Request, device_id: int, other_id: str = Form(""), side: str = Form("b"),
              my_port: str = Form(""), other_port: str = Form(""), label: str = Form(""),
              sess: dict = Depends(require_admin)):
    """side=b: to urządzenie jest wpięte w drugie (drugie bliżej routera); side=a: odwrotnie."""
    def add(c):
        other = _int(other_id)
        if not other:
            raise inv.InventoryError("Wybierz drugie urządzenie.")
        if side == "a":
            inv.add_cable(c, device_id, other, my_port, other_port, label)
        else:
            inv.add_cable(c, other, device_id, other_port, my_port, label)
    return _card_change(request, device_id, add, "cable_add", sess)


@router.post("/urzadzenia/{device_id}/kable/{cable_id}/usun")
def cable_remove(request: Request, device_id: int, cable_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: c.execute(cable.delete().where(
        cable.c.id == cable_id, or_(cable.c.a_device_id == device_id, cable.c.b_device_id == device_id))),
        "cable_remove", sess)


# ───────────────────────── ustawienia (wyjątki od profilu) ─────────────────────────
def parse_setting(key, raw):
    if key not in profiles.SETTINGS:
        raise profiles.ProfileError("Nieznane ustawienie.")
    typ = profiles.SETTINGS[key][0]
    if typ is bool:
        return raw == "tak"
    if typ is int:
        try:
            return int(raw)
        except ValueError:
            raise profiles.ProfileError("Podaj liczbę sekund.") from None
    return raw


@router.post("/urzadzenia/{device_id}/ustawienia")
def setting_change(request: Request, device_id: int, key: str = Form(""), value: str = Form(""),
                   clear: str = Form(""), sess: dict = Depends(require_admin)):
    def change(c):
        if key not in EDIT_SETTINGS:
            raise profiles.ProfileError("Tego ustawienia nie zmienia się tutaj.")
        if clear:
            profiles.clear_override(c, device_id, key)
        else:
            profiles.set_override(c, device_id, key, parse_setting(key, value))
    return _card_change(request, device_id, change, "setting_override", sess, key)


# ───────────────────────── pliki ─────────────────────────
@router.post("/urzadzenia/{device_id}/pliki")
async def file_upload(request: Request, device_id: int, plik: UploadFile = File(...),
                      sess: dict = Depends(require_admin)):
    data = await plik.read(files.MAX_SIZE + 1)
    return _card_change(request, device_id, lambda c: files.store(
        c, request.app.state.settings.data_dir, device_id, plik.filename, data, plik.content_type or "",
        user=sess["login"]), "file_upload", sess, plik.filename or "")


@router.get("/pliki/{file_id}")
def file_download(request: Request, file_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        found = files.get(conn, request.app.state.settings.data_dir, file_id)
    if not found or not found[1].exists():
        raise NotFound("Nie ma takiego pliku.")
    row, path = found
    # zawsze jako załącznik: wgrany HTML/SVG nie wykona się w kontekście panelu
    return FileResponse(path, media_type=row["mime"], filename=row["name"], content_disposition_type="attachment")


@router.post("/urzadzenia/{device_id}/pliki/{file_id}/usun")
def file_remove(request: Request, device_id: int, file_id: int, sess: dict = Depends(require_admin)):
    def rm(c):
        found = files.get(c, request.app.state.settings.data_dir, file_id)
        if found and found[0]["device_id"] == device_id:
            files.remove(c, request.app.state.settings.data_dir, file_id)
    return _card_change(request, device_id, rm, "file_remove", sess)


# ───────────────────────── skarbiec ─────────────────────────
@router.post("/urzadzenia/{device_id}/skarbiec")
def secret_add(request: Request, device_id: int, label: str = Form(""), username: str = Form(""),
               secret: str = Form(""), note: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            vault.add(conn, request.app.state.box, label, secret, role=sess["role"], user=sess["login"],
                      ip=client_ip(request), device_id=device_id, username=username, note=note)
        except ValueError as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
    return _back(f"/urzadzenia/{device_id}#skarbiec")


@router.post("/urzadzenia/{device_id}/skarbiec/{secret_id}/odslon")
def secret_reveal(request: Request, device_id: int, secret_id: int, sess: dict = Depends(require_admin)):
    if not auth.reauth_fresh(sess):
        raise Redirect(f"/potwierdz?next=/urzadzenia/{device_id}%23skarbiec")
    with request.app.state.engine.begin() as conn:
        try:
            plain = vault.reveal(conn, request.app.state.box, secret_id, role=sess["role"],
                                 reauth_at=sess["reauth_at"], user=sess["login"], ip=client_ip(request))
        except (ValueError, PermissionError) as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
        return _card(request, conn, device_id, revealed={"id": secret_id, "value": plain})


@router.post("/urzadzenia/{device_id}/skarbiec/{secret_id}/usun")
def secret_remove(request: Request, device_id: int, secret_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            vault.remove(conn, secret_id, role=sess["role"], user=sess["login"], ip=client_ip(request))
        except ValueError as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
    return _back(f"/urzadzenia/{device_id}#skarbiec")


# ───────────────────────── kolejka „do zatwierdzenia” ─────────────────────────
def _queue_page(request, conn, error=None, status_code=200):
    sources = {r.id: r.name for r in conn.execute(select(integration.c.id, integration.c.name))}
    all_devices = conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()
    return render(request, "kolejka.html", {"rows": discovery.pending(conn), "integrations": sources,
                                            "matches": discovery.suggest_matches(conn),
                                            "all_devices": all_devices, "error": error}, status_code)


@router.get("/zatwierdz")
def queue(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return _queue_page(request, conn)


def _queue_action(request, sess, fn):
    with request.app.state.engine.begin() as conn:
        try:
            fn(conn)
        except inv.InventoryError as e:
            return _queue_page(request, conn, str(e), 400)
    return _back("/zatwierdz")


@router.post("/zatwierdz/dopasowane")
def queue_link_matches(request: Request, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.link_matches(c, user=sess["login"], ip=client_ip(request)))


@router.post("/zatwierdz/wszystko")
def queue_approve_all(request: Request, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.approve_all(c, user=sess["login"], ip=client_ip(request)))


@router.post("/zatwierdz/{ref_id}")
def queue_approve(request: Request, ref_id: int, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.approve(c, ref_id, user=sess["login"],
                                                                    ip=client_ip(request)))


@router.post("/zatwierdz/{ref_id}/odrzuc")
def queue_ignore(request: Request, ref_id: int, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.ignore(c, ref_id, user=sess["login"],
                                                                   ip=client_ip(request)))


@router.post("/zatwierdz/{ref_id}/przypnij")
def queue_link(request: Request, ref_id: int, device_id: str = Form(""), sess: dict = Depends(require_admin)):
    def link(c):
        target = _int(device_id)
        if not target:
            raise inv.InventoryError("Wybierz urządzenie, do którego przypiąć.")
        discovery.link(c, ref_id, target, user=sess["login"], ip=client_ip(request))
    return _queue_action(request, sess, link)


# ───────────────────────── wyszukiwarka ─────────────────────────
def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/szukaj")
def search(request: Request, q: str = "", sess: dict = Depends(require_user)):
    q = q.strip()[:100]
    res = {"devices": [], "procedures": [], "runbooks": [], "incidents": []}
    if q:
        pat = _like(q)
        with request.app.state.engine.connect() as conn:
            by_addr = select(device_address.c.device_id).where(device_address.c.value.ilike(pat, escape="\\"))
            res["devices"] = conn.execute(select(device).where(or_(
                device.c.name.ilike(pat, escape="\\"), device.c.role.ilike(pat, escape="\\"),
                device.c.place.ilike(pat, escape="\\"), device.c.id.in_(by_addr))).order_by(device.c.name).limit(30)
            ).mappings().all()
            in_steps = select(procedure_step.c.procedure_id).where(or_(
                procedure_step.c.title.ilike(pat, escape="\\"), procedure_step.c.body_md.ilike(pat, escape="\\")))
            res["procedures"] = conn.execute(select(procedure).where(or_(
                procedure.c.title.ilike(pat, escape="\\"), procedure.c.intro_md.ilike(pat, escape="\\"),
                procedure.c.id.in_(in_steps))).order_by(procedure.c.title).limit(20)).mappings().all()
            res["runbooks"] = conn.execute(select(runbook).where(or_(
                runbook.c.title.ilike(pat, escape="\\"), runbook.c.body_md.ilike(pat, escape="\\")))
                .order_by(runbook.c.title).limit(20)).mappings().all()
            res["incidents"] = conn.execute(select(incident).where(or_(
                incident.c.title.ilike(pat, escape="\\"), incident.c.cause_md.ilike(pat, escape="\\"),
                incident.c.fix_md.ilike(pat, escape="\\"))).order_by(incident.c.started_at.desc()).limit(20)
            ).mappings().all()
    return render(request, "szukaj.html", {"q": q, "res": res, "total": sum(len(v) for v in res.values())})

