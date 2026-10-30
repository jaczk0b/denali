"""Incydenty (log problemów), procedury, runbooki i kontakty."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import auth, docs, incidents, inventory as inv
from ..models import contact, device, runbook, runbook_device
from ..templating import render
from .deps import NotFound, client_ip, require_admin, require_user

router = APIRouter()
MAX_STEPS = 30


def _int(v):
    try:
        return int(v) if v is not None and str(v).strip() else None
    except (TypeError, ValueError):
        return None


def _devices(conn):
    return conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()


def _ids(form, key):
    return [i for i in (_int(v) for v in form.getlist(key)) if i]


# ───────────────────────── incydenty ─────────────────────────
@router.get("/incydenty")
def incident_list(request: Request, wszystkie: str = "", sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        rows = incidents.list_incidents(conn, open_only=not wszystkie, limit=200)
    return render(request, "incydenty.html", {"rows": rows, "all": bool(wszystkie)})


def _incident_page(request, conn, incident_id, error=None, status_code=200):
    try:
        inc = incidents.get_incident(conn, incident_id)
    except incidents.IncidentError as e:
        raise NotFound(str(e)) from None
    procs = docs.procedures_for_device(conn, inc["root_device_id"]) if inc["root_device_id"] else []
    rbs = docs.runbooks_for_device(conn, inc["root_device_id"]) if inc["root_device_id"] else []
    return render(request, "incydent.html", {"inc": inc, "procedures": procs, "runbooks": rbs, "error": error},
                  status_code)


@router.get("/incydenty/{incident_id:int}")
def incident_view(request: Request, incident_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return _incident_page(request, conn, incident_id)


@router.get("/incydenty/nowy")
def incident_new_form(request: Request, urzadzenie: str = "", sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return render(request, "incydent_nowy.html", {"devices": _devices(conn), "device_id": _int(urzadzenie),
                                                      "error": None, "form": {}})


@router.post("/incydenty/nowy")
async def incident_create(request: Request, sess: dict = Depends(require_admin)):
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        try:
            iid = incidents.open_manual(conn, form.get("title", ""), sess["login"],
                                        device_id=_int(form.get("device_id")),
                                        severity=form.get("severity", "ostrzezenie"), text=form.get("text", ""))
        except incidents.IncidentError as e:
            return render(request, "incydent_nowy.html", {"devices": _devices(conn), "error": str(e),
                                                          "device_id": _int(form.get("device_id")), "form": form}, 400)
        auth.audit(conn, "incident_create", user=sess["login"], ip=client_ip(request), target=str(iid))
    return RedirectResponse(f"/incydenty/{iid}", 303)


def _incident_change(request, incident_id, fn):
    with request.app.state.engine.begin() as conn:
        try:
            fn(conn)
        except incidents.IncidentError as e:
            return _incident_page(request, conn, incident_id, str(e), 400)
    return RedirectResponse(f"/incydenty/{incident_id}", 303)


@router.post("/incydenty/{incident_id}/potwierdz")
def incident_ack(request: Request, incident_id: int, sess: dict = Depends(require_admin)):
    return _incident_change(request, incident_id, lambda c: incidents.ack(c, incident_id, sess["login"]))


@router.post("/incydenty/{incident_id}/notatka")
def incident_note(request: Request, incident_id: int, text: str = Form(""), sess: dict = Depends(require_admin)):
    return _incident_change(request, incident_id, lambda c: incidents.add_note(c, incident_id, text, sess["login"]))


@router.post("/incydenty/{incident_id}/zamknij")
def incident_close(request: Request, incident_id: int, cause_md: str = Form(""), fix_md: str = Form(""),
                   sess: dict = Depends(require_admin)):
    return _incident_change(request, incident_id, lambda c: incidents.close(c, incident_id, sess["login"],
                                                                            cause_md=cause_md, fix_md=fix_md))


@router.post("/incydenty/{incident_id}/wznow")
def incident_reopen(request: Request, incident_id: int, sess: dict = Depends(require_admin)):
    return _incident_change(request, incident_id, lambda c: incidents.reopen(c, incident_id, sess["login"]))


# ───────────────────────── procedury ─────────────────────────
@router.get("/procedury")
def procedure_list(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        rows = docs.list_procedures(conn)
        contacts = conn.execute(select(contact).order_by(contact.c.name)).mappings().all()
    return render(request, "procedury.html", {"rows": rows, "contacts": contacts})


@router.get("/procedury/{procedure_id:int}")
def procedure_view(request: Request, procedure_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        p = docs.get_procedure(conn, procedure_id)
    if not p:
        raise NotFound("Nie ma takiej procedury.")
    return render(request, "procedura.html", {"p": p})


def _procedure_form(request, conn, p, error=None, status_code=200):
    contacts = conn.execute(select(contact).order_by(contact.c.name)).mappings().all()
    steps = list(p.get("steps") or []) + [{"title": "", "body_md": ""}] * 3   # 3 puste pola na nowe kroki (bez JS)
    return render(request, "procedura_edycja.html", {"p": p, "steps": steps[:MAX_STEPS], "devices": _devices(conn),
                                                     "contacts": contacts, "error": error, "scopes": docs.SCOPES},
                  status_code)


@router.get("/procedury/nowa")
def procedure_new_form(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _procedure_form(request, conn, {"id": None, "title": "", "scope": "infrastruktura", "intro_md": "",
                                               "trigger": "", "steps": [], "devices": [], "contacts": []})


@router.get("/procedury/{procedure_id}/edycja")
def procedure_edit_form(request: Request, procedure_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        p = docs.get_procedure(conn, procedure_id)
        if not p:
            raise NotFound("Nie ma takiej procedury.")
        return _procedure_form(request, conn, p)


async def _procedure_save(request, sess, procedure_id=None):
    form = await request.form()
    titles, bodies = form.getlist("step_title"), form.getlist("step_body")
    steps = [(t, b) for t, b in zip(titles, bodies) if t.strip() or b.strip()][:MAX_STEPS]
    device_ids, contact_ids = _ids(form, "device_ids"), _ids(form, "contact_ids")
    with request.app.state.engine.begin() as conn:
        try:
            pid = docs.save_procedure(conn, form.get("title", ""), steps, scope=form.get("scope", "infrastruktura"),
                                      intro_md=form.get("intro_md", ""), trigger=form.get("trigger", ""),
                                      device_ids=device_ids, contact_ids=contact_ids, user=sess["login"],
                                      procedure_id=procedure_id)
        except docs.DocsError as e:
            p = {"id": procedure_id, "title": form.get("title", ""), "scope": form.get("scope", ""),
                 "intro_md": form.get("intro_md", ""), "trigger": form.get("trigger", ""),
                 "steps": [{"title": t, "body_md": b} for t, b in steps],
                 "devices": [{"id": i} for i in device_ids], "contacts": [{"id": i} for i in contact_ids]}
            return _procedure_form(request, conn, p, str(e), 400)
        auth.audit(conn, "procedure_save", user=sess["login"], ip=client_ip(request), target=form.get("title", ""))
    return RedirectResponse(f"/procedury/{pid}", 303)


@router.post("/procedury/nowa")
async def procedure_create(request: Request, sess: dict = Depends(require_admin)):
    return await _procedure_save(request, sess)


@router.post("/procedury/{procedure_id}/edycja")
async def procedure_update(request: Request, procedure_id: int, sess: dict = Depends(require_admin)):
    return await _procedure_save(request, sess, procedure_id)


@router.post("/procedury/{procedure_id}/sprawdzona")
def procedure_tested(request: Request, procedure_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        docs.mark_tested(conn, procedure_id)
        auth.audit(conn, "procedure_tested", user=sess["login"], ip=client_ip(request), target=str(procedure_id))
    return RedirectResponse(f"/procedury/{procedure_id}", 303)


@router.post("/procedury/{procedure_id}/usun")
def procedure_delete(request: Request, procedure_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        docs.delete_procedure(conn, procedure_id)
        auth.audit(conn, "procedure_delete", user=sess["login"], ip=client_ip(request), target=str(procedure_id))
    return RedirectResponse("/procedury", 303)


# ───────────────────────── kontakty ─────────────────────────
@router.post("/kontakty")
def contact_add(request: Request, name: str = Form(""), phone: str = Form(""), note: str = Form(""),
                sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            docs.save_contact(conn, name, phone, note)
        except docs.DocsError as e:
            rows = docs.list_procedures(conn)
            contacts = conn.execute(select(contact).order_by(contact.c.name)).mappings().all()
            return render(request, "procedury.html", {"rows": rows, "contacts": contacts, "error": str(e)}, 400)
    return RedirectResponse("/procedury#kontakty", 303)


@router.post("/kontakty/{contact_id}/usun")
def contact_delete(request: Request, contact_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        conn.execute(contact.delete().where(contact.c.id == contact_id))
    return RedirectResponse("/procedury#kontakty", 303)


# ───────────────────────── runbooki ─────────────────────────
def _runbook(conn, runbook_id):
    r = conn.execute(select(runbook).where(runbook.c.id == runbook_id)).mappings().first()
    if not r:
        raise NotFound("Nie ma takiego runbooka.")
    out = dict(r)
    out["device_ids"] = [x.device_id for x in conn.execute(
        select(runbook_device.c.device_id).where(runbook_device.c.runbook_id == runbook_id))]
    return out


@router.get("/runbooki/{runbook_id:int}")
def runbook_view(request: Request, runbook_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        r = _runbook(conn, runbook_id)
        names = {d["id"]: d["name"] for d in _devices(conn)}
    return render(request, "runbook.html", {"r": r, "names": names})


@router.get("/runbooki/nowy")
def runbook_new_form(request: Request, urzadzenie: str = "", sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        did = _int(urzadzenie)
        return render(request, "runbook_edycja.html", {"r": {"id": None, "title": "", "body_md": "",
                                                             "device_ids": [did] if did else []},
                                                       "devices": _devices(conn), "error": None})


@router.get("/runbooki/{runbook_id}/edycja")
def runbook_edit_form(request: Request, runbook_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return render(request, "runbook_edycja.html", {"r": _runbook(conn, runbook_id), "devices": _devices(conn),
                                                       "error": None})


async def _runbook_save(request, sess, runbook_id=None):
    form = await request.form()
    device_ids = _ids(form, "device_ids")
    with request.app.state.engine.begin() as conn:
        try:
            rid = docs.save_runbook(conn, form.get("title", ""), form.get("body_md", ""), device_ids,
                                    user=sess["login"], runbook_id=runbook_id)
        except docs.DocsError as e:
            r = {"id": runbook_id, "title": form.get("title", ""), "body_md": form.get("body_md", ""),
                 "device_ids": device_ids}
            return render(request, "runbook_edycja.html", {"r": r, "devices": _devices(conn), "error": str(e)}, 400)
        auth.audit(conn, "runbook_save", user=sess["login"], ip=client_ip(request), target=form.get("title", ""))
    return RedirectResponse(f"/runbooki/{rid}", 303)


@router.post("/runbooki/nowy")
async def runbook_create(request: Request, sess: dict = Depends(require_admin)):
    return await _runbook_save(request, sess)


@router.post("/runbooki/{runbook_id}/edycja")
async def runbook_update(request: Request, runbook_id: int, sess: dict = Depends(require_admin)):
    return await _runbook_save(request, sess, runbook_id)


@router.post("/runbooki/{runbook_id}/usun")
def runbook_delete(request: Request, runbook_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        docs.delete_runbook(conn, runbook_id)
        auth.audit(conn, "runbook_delete", user=sess["login"], ip=client_ip(request), target=str(runbook_id))
    return RedirectResponse("/urzadzenia", 303)
