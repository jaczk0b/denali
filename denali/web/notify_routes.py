"""Kanały powiadomień (tylko administrator): dodanie, wysyłka testowa, włącz/wyłącz, usuń, powtórki,
podsumowanie tygodnia na żądanie."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete

from .. import auth, notify, watchdog, weekly
from ..models import notification_channel
from ..templating import render
from .deps import client_ip, require_admin

router = APIRouter()


def _transport(request):
    return getattr(request.app.state, "notify_transport", None)    # w testach: udawany Discord


def _page(request, conn, error=None, message=None, status_code=200):
    return render(request, "powiadomienia.html", {"rows": notify.channels(conn), "error": error, "message": message,
                                                  "labels": notify.SEVERITY_LABEL, "watchdog": watchdog.status(conn),
                                                  "types": notify.CHANNEL_TYPES, "repeat": notify.repeat_hours(conn),
                                                  "repeat_choices": notify.REPEAT_CHOICES},
                  status_code)


@router.get("/admin/powiadomienia")
def channels(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _page(request, conn)


@router.post("/admin/powiadomienia")
async def channel_add(request: Request, name: str = Form(""), webhook: str = Form(""),
                      min_severity: str = Form("ostrzezenie"), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            cid = notify.save_channel(conn, request.app.state.box, name, webhook, min_severity)
        except notify.NotifyError as e:
            return _page(request, conn, error=str(e), status_code=400)
        auth.audit(conn, "channel_create", user=sess["login"], ip=client_ip(request), target=name)
        try:
            await notify.test_channel(conn, request.app.state.box, cid, request.app.state.settings.base_url,
                                      _transport(request))
        except notify.NotifyError as e:
            return _page(request, conn, error=f"Kanał zapisany, ale wiadomość testowa nie doszła: {e}")
        return _page(request, conn, message="Zapisane. Na Discordzie powinna być wiadomość testowa.")


@router.post("/admin/powiadomienia/inny")
async def channel_add_other(request: Request, sess: dict = Depends(require_admin)):
    form = await request.form()
    type_ = str(form.get("type") or "")
    with request.app.state.engine.begin() as conn:
        try:
            cid = notify.save_other(conn, request.app.state.box, type_, str(form.get("name") or ""), form,
                                    str(form.get("min_severity") or "ostrzezenie"))
        except notify.NotifyError as e:
            return _page(request, conn, error=str(e), status_code=400)
        auth.audit(conn, "channel_create", user=sess["login"], ip=client_ip(request), target=type_)
        try:
            await notify.test_channel(conn, request.app.state.box, cid, request.app.state.settings.base_url,
                                      _transport(request))
        except notify.NotifyError as e:
            return _page(request, conn, error=f"Kanał zapisany, ale wiadomość testowa nie doszła: {e}")
        return _page(request, conn, message="Zapisane. Powinna przyjść wiadomość testowa.")


@router.post("/admin/powiadomienia/powtorki")
def repeat_set(request: Request, hours: int = Form(0), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            notify.set_repeat_hours(conn, hours)
        except notify.NotifyError as e:
            return _page(request, conn, error=str(e), status_code=400)
        auth.audit(conn, "notify_repeat", user=sess["login"], ip=client_ip(request), params={"godziny": hours})
    return RedirectResponse("/admin/powiadomienia", 303)


@router.post("/admin/powiadomienia/straznik")
async def watchdog_set(request: Request, url: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            watchdog.configure(conn, request.app.state.box, url)
        except watchdog.WatchdogError as e:
            return _page(request, conn, error=str(e), status_code=400)
        auth.audit(conn, "watchdog_set" if url.strip() else "watchdog_off", user=sess["login"], ip=client_ip(request))
        if not url.strip():
            return _page(request, conn, message="Strażnik wyłączony.")
        ok = await watchdog.ping(conn, request.app.state.box, getattr(request.app.state, "watchdog_transport", None),
                                 force=True)
        st = watchdog.status(conn)
        if not ok:
            return _page(request, conn, error=f"Zapisane, ale pierwszy meldunek nie doszedł: {st['last_error']}")
        return _page(request, conn, message="Zapisane — pierwszy meldunek doszedł. Od teraz co minutę.")


@router.post("/admin/powiadomienia/podsumowanie")
async def weekly_now(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        sent = await weekly.process(conn, request.app.state.box, request.app.state.settings.base_url,
                                    _transport(request), force=True)
        auth.audit(conn, "weekly_send", user=sess["login"], ip=client_ip(request), result="ok" if sent else "blad")
        if not sent:
            return _page(request, conn, error="Nie wysłano — brak włączonego kanału albo Discord odrzucił wiadomość.",
                         status_code=502)
        return _page(request, conn, message="Wysłano podsumowanie ostatnich 7 dni.")


@router.post("/admin/powiadomienia/{channel_id:int}/test")
async def channel_test(request: Request, channel_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        try:
            await notify.test_channel(conn, request.app.state.box, channel_id, request.app.state.settings.base_url,
                                      _transport(request))
        except notify.NotifyError as e:
            return _page(request, conn, error=str(e), status_code=502)
        return _page(request, conn, message="Wysłano wiadomość testową.")


@router.post("/admin/powiadomienia/{channel_id:int}/{action}")
def channel_change(request: Request, channel_id: int, action: str, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        if action == "usun":
            conn.execute(delete(notification_channel).where(notification_channel.c.id == channel_id))
        elif action in ("wlacz", "wylacz"):
            conn.execute(notification_channel.update().where(notification_channel.c.id == channel_id)
                         .values(enabled=action == "wlacz"))
        else:
            return _page(request, conn, error="Nieznana akcja.", status_code=400)
        auth.audit(conn, f"channel_{action}", user=sess["login"], ip=client_ip(request), target=str(channel_id))
    return RedirectResponse("/admin/powiadomienia", 303)
