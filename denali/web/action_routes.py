"""Akcje w panelu (tylko administrator): potwierdzenie i zlecenie, przebieg, historia, wyłącznik, token do akcji
w integracji, listy „Dozwolone akcje” w profilu i na urządzeniu.

O tym, czy akcja może się wykonać, decyduje wyłącznie actions.request — te trasy tylko zbierają dane z formularza.
Zmiana tego, co wolno (wyłącznik, token, listy dozwolonych), wymaga świeżego potwierdzenia hasłem i kodem.
"""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import actions, auth, integrations, inventory as inv, profiles
from ..models import integration, profile
from ..templating import render
from .deps import NotFound, Redirect, client_ip, require_admin

router = APIRouter()


def _transport(request):
    return getattr(request.app.state, "adapter_transport", None)   # w testach: nagrane odpowiedzi API


def _need_reauth(sess, back):
    if not auth.reauth_fresh(sess):
        raise Redirect(f"/potwierdz?next={quote(back, safe='/#?=&')}")


def _confirm_page(request, conn, device_id, ref_id, key, error=None, status_code=200):
    d = inv.get_device(conn, device_id)
    if not d:
        raise NotFound("Nie ma takiego urządzenia.")
    try:
        o = actions.find(conn, device_id, ref_id, key)
    except actions.ActionError as e:
        raise NotFound(str(e)) from None
    snap = actions.snapshot_option(conn, device_id) if o["cls"].push else None
    return render(request, "akcja.html", {
        "d": d, "o": o, "error": error, "snap": snap, "fresh": auth.reauth_fresh(request.state.sess),
        "here": f"/urzadzenia/{device_id}/akcje/{ref_id}/{key}"}, status_code)


@router.get("/urzadzenia/{device_id:int}/akcje/{ref_id:int}/{key}")
def action_confirm(request: Request, device_id: int, ref_id: int, key: str, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _confirm_page(request, conn, device_id, ref_id, key)


@router.post("/urzadzenia/{device_id:int}/akcje/{ref_id:int}/{key}")
async def action_start(request: Request, device_id: int, ref_id: int, key: str, sess: dict = Depends(require_admin)):
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        try:
            run_id = actions.request(conn, device_id=device_id, ref_id=ref_id, key=key, user=sess["login"],
                                     role=sess["role"], ip=client_ip(request), reauth_ok=auth.reauth_fresh(sess),
                                     snapshot_first=form.get("snapshot") == "1")
        except actions.ActionError as e:
            error = str(e)
        else:
            error = None
    if error:
        with request.app.state.engine.connect() as conn:
            if not inv.get_device(conn, device_id):
                raise NotFound("Nie ma takiego urządzenia.")
            try:
                return _confirm_page(request, conn, device_id, ref_id, key, error, 403)
            except NotFound:
                return render(request, "blad.html", {"title": "Akcja niemożliwa", "text": error}, 403)
    await actions.execute(request.app.state.engine, request.app.state.box, run_id, _transport(request))
    return RedirectResponse(f"/akcje/{run_id}", 303)


# ───────────────────────── przebieg i historia ─────────────────────────
@router.get("/akcje")
def action_list(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        rows = actions.recent(conn, limit=200)
        ints = conn.execute(select(integration).order_by(integration.c.name)).mappings().all()
        types = actions.registry()
        tokens = [{"row": r, "cls": types[r["type"]], "ready": actions.has_credentials(r)}
                  for r in ints if r["type"] in types and types[r["type"]].actions]
        on = actions.enabled(conn)
    return render(request, "akcje.html", {"rows": rows, "tokens": tokens, "on": on, "labels": actions.STATUS_LABEL,
                                          "fresh": auth.reauth_fresh(sess)})


@router.get("/akcje/{run_id:int}")
def action_view(request: Request, run_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        run = actions.get(conn, run_id)
        if not run:
            raise NotFound("Nie ma takiej akcji.")
        chain = [r for r in (actions.get(conn, run["after_id"]) if run["after_id"] else None,) if r]
        chain += conn.execute(select(actions.action_run).where(actions.action_run.c.after_id == run_id)
                              ).mappings().all()
    return render(request, "akcja_przebieg.html", {"run": run, "chain": chain, "labels": actions.STATUS_LABEL,
                                                   "active": run["status"] in actions.ACTIVE})


@router.post("/akcje/wlacz")
def actions_on(request: Request, sess: dict = Depends(require_admin)):
    _need_reauth(sess, "/akcje")
    with request.app.state.engine.begin() as conn:
        actions.set_enabled(conn, True, sess["login"], client_ip(request))
    return RedirectResponse("/akcje", 303)


@router.post("/akcje/wylacz")
def actions_off(request: Request, sess: dict = Depends(require_admin)):
    # wyłączenie bez potwierdzenia — ma działać od razu, gdy coś jest nie tak
    with request.app.state.engine.begin() as conn:
        actions.set_enabled(conn, False, sess["login"], client_ip(request))
    return RedirectResponse("/akcje", 303)


# ───────────────────────── token do akcji w integracji ─────────────────────────
def _integration(conn, integration_id):
    try:
        return integrations.get(conn, integration_id)
    except integrations.IntegrationError as e:
        raise NotFound(str(e)) from None


@router.post("/admin/integracje/{integration_id:int}/akcje")
async def action_token_save(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    back = f"/admin/integracje/{integration_id}#akcje"
    _need_reauth(sess, back)
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        row = _integration(conn, integration_id)
        try:
            actions.save_credentials(conn, request.app.state.box, row, form, sess["login"], client_ip(request))
        except actions.ActionError as e:
            return render(request, "blad.html", {"title": "Token do akcji niezapisany", "text": str(e),
                                                 "back": back}, 400)
    return RedirectResponse(back, 303)


@router.post("/admin/integracje/{integration_id:int}/akcje/usun")
def action_token_remove(request: Request, integration_id: int, sess: dict = Depends(require_admin)):
    # usunięcie tokenu = wyłączenie akcji tej integracji; bez potwierdzenia, jak wyłącznik
    with request.app.state.engine.begin() as conn:
        actions.clear_credentials(conn, _integration(conn, integration_id), sess["login"], client_ip(request))
    return RedirectResponse(f"/admin/integracje/{integration_id}#akcje", 303)


# ───────────────────────── listy „Dozwolone akcje” ─────────────────────────
def _keys(form):
    return [str(k) for k in form.getlist("allow")]


@router.post("/urzadzenia/{device_id:int}/akcje-dozwolone")
async def device_allowed(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    back = f"/urzadzenia/{device_id}#akcje"
    _need_reauth(sess, back)
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        if not inv.get_device(conn, device_id):
            raise NotFound("Nie ma takiego urządzenia.")
        try:
            if form.get("clear") == "1":
                profiles.clear_override(conn, device_id, "actions")
            else:
                # klucze spoza tego urządzenia zostają takie, jak były (np. z profilu) — formularz pokazuje tylko jego
                known = {k for k, _s, _c in actions.known_for_device(conn, device_id)}
                current = set(profiles.value(conn, device_id, "actions"))
                profiles.set_override(conn, device_id, "actions",
                                      sorted((current - known) | (set(_keys(form)) & known)))
        except profiles.ProfileError as e:
            return render(request, "blad.html", {"title": "Nie zapisano", "text": str(e), "back": back}, 400)
        auth.audit(conn, "actions_allowed_device", user=sess["login"], ip=client_ip(request), target=str(device_id),
                   params={"actions": profiles.value(conn, device_id, "actions")})
    return RedirectResponse(back, 303)


@router.post("/admin/profile/{profile_id:int}/akcje")
async def profile_allowed(request: Request, profile_id: int, sess: dict = Depends(require_admin)):
    _need_reauth(sess, "/admin/profile")
    form = await request.form()
    with request.app.state.engine.begin() as conn:
        p = conn.execute(select(profile).where(profile.c.id == profile_id)).mappings().first()
        if not p:
            raise NotFound("Nie ma takiego profilu.")
        try:
            profiles.set_profile_setting(conn, profile_id, "actions", _keys(form))
        except profiles.ProfileError as e:
            return render(request, "blad.html", {"title": "Nie zapisano", "text": str(e), "back": "/admin/profile"}, 400)
        auth.audit(conn, "actions_allowed_profile", user=sess["login"], ip=client_ip(request), target=p["name"],
                   params={"actions": sorted(set(_keys(form)))})
    return RedirectResponse("/admin/profile", 303)
