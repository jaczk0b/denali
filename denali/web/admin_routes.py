"""Ponowne potwierdzenie hasłem (skarbiec), parowanie tabletu (kiosk), PIN i odblokowanie tabletu, profile."""
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select

from .. import actions, auth, notify, profiles, tablet
from ..db import kv_get, kv_set
from ..models import app_user, device, profile, session
from ..templating import render
from .auth_routes import _secure
from .deps import (COOKIE, PRELOGIN_COOKIE, RESUME_PATH, Forbidden, Redirect, client_ip, get_session, require_admin,
                   require_fresh, require_user, safe_next, set_cookie, take_pending)
from .infra_routes import EDIT_SETTINGS, parse_setting

router = APIRouter()
KIOSK_LOGIN = "tablet"
PAIR_TTL = 600                                      # kod parowania ważny 10 minut
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # bez 0/O i 1/I/L


def _hash(code):
    return hashlib.sha256(code.strip().upper().replace("-", "").replace(" ", "").encode()).hexdigest()


# ───────────────────────── ponowne potwierdzenie ─────────────────────────
def _user(request: Request, sess: dict):
    with request.app.state.engine.connect() as conn:
        return conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()


def _reauth_page(request: Request, sess: dict, nxt: str, error: str | None = None, status: int = 200):
    user = _user(request, sess)
    oidc_on = request.app.state.settings.oidc_enabled and bool(user["oidc_sub"])
    return render(request, "potwierdz.html", {"next": nxt, "error": error, "has_password": auth.has_password(user),
                                              "oidc_label": request.app.state.settings.oidc_label if oidc_on else ""},
                  status)


def _to_provider(request: Request, url: str):
    """Wyjście do Pocket ID przez stronę, a nie przekierowanie. Do /potwierdz zwykle trafia się z formularza, a CSP
    „form-action 'self'” zatrzymuje formularz, którego przekierowania wychodzą poza panel: przeglądarka zostaje
    wtedy na miejscu, bez żadnego komunikatu."""
    return render(request, "przejscie.html", {"url": url})


@router.get("/potwierdz")
def reauth_form(request: Request, next: str = "", inna: str = "", blad: str = "", sess: dict = Depends(require_user)):
    nxt, user = safe_next(next), _user(request, sess)
    prefers_oidc = user["reauth_method"] == "oidc" or not auth.has_password(user)
    if not inna and prefers_oidc and user["oidc_sub"] and request.app.state.settings.oidc_enabled:
        return _to_provider(request, f"/login/oidc?cel=reauth&next={quote(nxt, safe='')}")
    return _reauth_page(request, sess, nxt, "Pocket ID nie potwierdził. Potwierdź inaczej." if blad else None)


@router.post("/potwierdz")
def reauth(request: Request, password: str = Form(""), code: str = Form(""), next: str = Form(""),
           sess: dict = Depends(require_user)):
    nxt, ip = safe_next(next), client_ip(request)
    with request.app.state.engine.begin() as conn:
        if auth.login_blocked(conn, sess["login"], ip):
            return _reauth_page(request, sess, nxt, "Za dużo nieudanych prób. Spróbuj za 15 minut.", 429)
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        ok = auth.verify_password(user["pw_hash"], password)
        if ok and user["totp_ct"]:
            ok = bool(auth.check_second_factor(conn, user, code, request.app.state.box))
        if not ok:
            auth.record_attempt(conn, sess["login"], ip, False)
            auth.audit(conn, "reauth_failed", user=sess["login"], ip=ip, result="złe dane")
            return _reauth_page(request, sess, nxt, "Złe hasło albo kod.", 400)
        auth.mark_reauth(conn, sess["token"])
        auth.audit(conn, "reauth", user=sess["login"], ip=ip)
    if nxt.startswith("/login/oidc"):       # łączenie konta: dalej jest już strona dostawcy
        return _to_provider(request, nxt)
    return RedirectResponse(nxt or "/", 303)


@router.get(RESUME_PATH)
def reauth_resume(request: Request, sess: dict = Depends(require_user)):
    if not auth.reauth_fresh(sess):
        raise Redirect(f"/potwierdz?next={RESUME_PATH}")
    with request.app.state.engine.begin() as conn:
        pending = take_pending(conn, request.app.state.box, sess)
    if not pending:
        raise Redirect("/")
    return render(request, "dokoncz.html", {"pending": pending})


# ───────────────────────── tablet (kiosk) ─────────────────────────
def _kiosk_user(conn):
    u = auth.get_user(conn, KIOSK_LOGIN)
    if u:
        if u["role"] != "podglad":
            raise RuntimeError(f"Konto „{KIOSK_LOGIN}” istnieje i nie jest kontem podglądu — tablet go nie użyje.")
        return u["id"]
    # konto tylko do podglądu, z losowym hasłem, którego nikt nie zna — wejście wyłącznie kodem parowania
    return auth.create_user(conn, KIOSK_LOGIN, secrets.token_urlsafe(32), "podglad")


def _tablet_admin_page(request, conn, code=None, error=None, message=None):
    kiosks = conn.execute(select(session.c.public_id, session.c.ip, session.c.user_agent, session.c.created_at,
                                 session.c.last_seen_at, session.c.expires_at)
                          .where(session.c.kind == "kiosk").order_by(session.c.created_at)).mappings().all()
    pending = kv_get(conn, "kiosk.pair")
    active = pending and pending.get("expires", 0) > time.time()
    return render(request, "admin_tablet.html", {"code": code, "kiosks": kiosks, "error": error, "message": message,
                                                 "pins": {k["public_id"]: tablet.pin_info(conn, k["public_id"])
                                                          for k in kiosks},
                                                 "pending_until": pending["expires"] if active else None,
                                                 "max_fails": tablet.MAX_FAILS})


@router.get("/admin/tablet")
def tablet_admin(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _tablet_admin_page(request, conn)


@router.post("/admin/tablet/kod")
def tablet_code(request: Request, sess: dict = Depends(require_admin)):
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    with request.app.state.engine.begin() as conn:
        kv_set(conn, "kiosk.pair", {"hash": _hash(code), "expires": time.time() + PAIR_TTL, "by": sess["login"]})
        auth.audit(conn, "kiosk_code", user=sess["login"], ip=client_ip(request))
        return _tablet_admin_page(request, conn, code=f"{code[:4]}-{code[4:]}")


@router.post("/admin/tablet/{public_id}/usun")
def tablet_revoke(request: Request, public_id: str, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        conn.execute(delete(session).where(session.c.kind == "kiosk", session.c.public_id == public_id))
        auth.audit(conn, "kiosk_revoke", user=sess["login"], ip=client_ip(request), target=public_id)
    return RedirectResponse("/admin/tablet", 303)


@router.post("/admin/tablet/{public_id}/pin")
async def tablet_pin(request: Request, public_id: str, pin: str = Form(""), sess: dict = Depends(require_admin)):
    """PIN odblokowuje na tablecie pełny dostęp (SSH, VNC, akcje) — ustawienie wymaga świeżego hasła i kodu."""
    if sess.get("elevated"):
        raise Forbidden()                      # z odblokowanego tabletu nie zmienia się jego własnego PIN-u
    await require_fresh(request, sess)
    with request.app.state.engine.begin() as conn:
        try:
            tablet.set_pin(conn, public_id, pin, sess["login"])
        except tablet.TabletError as e:
            return _tablet_admin_page(request, conn, error=str(e))
        auth.audit(conn, "tablet_pin_set", user=sess["login"], ip=client_ip(request), target=public_id)
        return _tablet_admin_page(request, conn, message="PIN ustawiony. Na tablecie: przycisk „Odblokuj”.")


@router.post("/admin/tablet/{public_id}/pin/usun")
def tablet_pin_clear(request: Request, public_id: str, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        tablet.clear_pin(conn, public_id)
        auth.audit(conn, "tablet_pin_clear", user=sess["login"], ip=client_ip(request), target=public_id)
    return RedirectResponse("/admin/tablet", 303)


# ───────────────────────── na tablecie: odblokuj, zablokuj, ustawienia ─────────────────────────
def _kiosk(sess):
    if sess["kind"] != "kiosk":
        raise Forbidden()
    return sess


@router.post("/tablet/odblokuj")
async def tablet_unlock(request: Request, pin: str = Form(""), sess: dict = Depends(require_user)):
    _kiosk(sess)
    ip = client_ip(request)
    with request.app.state.engine.begin() as conn:
        try:
            tablet.unlock(conn, sess, pin, ip)
        except tablet.TabletError as e:
            kv_set(conn, f"tablet.flash.{sess['public_id']}", str(e))
            if getattr(e, "wiped", False):
                await notify.deliver(conn, request.app.state.box, {
                    "username": "Denali", "allowed_mentions": {"parse": []},
                    "embeds": [{"title": "Tablet: PIN skasowany po złych próbach",
                                "description": f"{tablet.MAX_FAILS} razy wpisano zły PIN na tablecie ({ip}). "
                                               "Ustaw nowy w panelu: Tablet na ścianie.",
                                "color": notify.COLORS["ostrzezenie"]}]}, "ostrzezenie",
                    getattr(request.app.state, "notify_transport", None))
            return RedirectResponse("/#odblokuj", 303)
    return RedirectResponse("/", 303)


@router.post("/tablet/zablokuj")
def tablet_lock(request: Request, sess: dict = Depends(require_user)):
    _kiosk(sess)
    with request.app.state.engine.begin() as conn:
        tablet.lock(conn, sess, client_ip(request))
    return RedirectResponse("/", 303)


@router.get("/tablet/aktywny")
def tablet_active(request: Request, sess: dict = Depends(require_user)):
    """Dotyk na odblokowanym tablecie przedłuża odblokowanie (samo żądanie to robi); zwraca, do kiedy."""
    _kiosk(sess)
    return JSONResponse({"until": sess.get("unlock_until")})


@router.post("/tablet/ustawienia")
def tablet_prefs(request: Request, mode: str = Form("panel"), quiet_from: str = Form("22"), quiet_to: str = Form("7"),
                 sess: dict = Depends(require_user)):
    _kiosk(sess)
    resp = RedirectResponse("/", 303)
    set_cookie(resp, tablet.PREFS_COOKIE, tablet.prefs_value(mode, quiet_from, quiet_to), auth.KIOSK_AGE,
               _secure(request))
    return resp


def _tablet_page(request, error=None, status_code=200):
    pre = secrets.token_urlsafe(24)
    resp = render(request, "tablet.html", {"pre": pre, "error": error}, status_code)
    set_cookie(resp, PRELOGIN_COOKIE, pre, 900, _secure(request))
    return resp


@router.get("/tablet")
def tablet_form(request: Request):
    sess = get_session(request)
    if sess and sess["kind"] == "kiosk":
        return RedirectResponse("/", 303)
    return _tablet_page(request)


@router.post("/tablet")
def tablet_pair(request: Request, code: str = Form(""), pre: str = Form("")):
    cookie = request.cookies.get(PRELOGIN_COOKIE, "")
    if not pre or not cookie or not hmac.compare_digest(pre, cookie):
        return _tablet_page(request, "Formularz wygasł. Spróbuj jeszcze raz.", 400)
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    with request.app.state.engine.begin() as conn:
        if auth.login_blocked(conn, KIOSK_LOGIN, ip):
            return _tablet_page(request, "Za dużo nieudanych prób. Spróbuj za 15 minut.", 429)
        pending = kv_get(conn, "kiosk.pair")
        ok = (bool(pending and pending.get("hash")) and pending["expires"] > time.time()
              and hmac.compare_digest(pending["hash"], _hash(code)))
        if not ok:
            auth.record_attempt(conn, KIOSK_LOGIN, ip, False)
            auth.audit(conn, "kiosk_pair_failed", user=KIOSK_LOGIN, ip=ip, result="zły kod")
            return _tablet_page(request, "Zły albo wygasły kod. Wygeneruj nowy w panelu: Tablet.", 400)
        kv_set(conn, "kiosk.pair", {})                 # kod działa tylko raz
        auth.record_attempt(conn, KIOSK_LOGIN, ip, True)
        token = auth.create_session(conn, _kiosk_user(conn), "kiosk", ip, ua)
        auth.audit(conn, "kiosk_paired", user=KIOSK_LOGIN, ip=ip, params={"przez": pending.get("by", "")})
    resp = RedirectResponse("/", 303)
    set_cookie(resp, COOKIE, token, auth.KIOSK_AGE, _secure(request))
    resp.delete_cookie(PRELOGIN_COOKIE, path="/")
    return resp


# ───────────────────────── profile ─────────────────────────
def _profiles_page(request, conn, error=None, status_code=200):
    rows = conn.execute(select(profile).order_by(profile.c.id)).mappings().all()
    devs = conn.execute(select(device.c.id, device.c.name, device.c.kind, device.c.profile_id)).mappings().all()
    usage = {p["id"]: 0 for p in rows}
    for d in devs:
        p = profiles.profile_for(conn, d)
        if p:
            usage[p["id"]] += 1
    return render(request, "admin_profile.html", {
        "rows": rows, "usage": usage, "defs": profiles.SETTINGS, "edit_settings": EDIT_SETTINGS, "error": error,
        "action_catalog": [(k, c, a) for k, (c, a) in actions.catalog().items() if not a.terminal],
}, status_code)


@router.get("/admin/profile")
def profiles_view(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _profiles_page(request, conn)


@router.post("/admin/profile/{profile_id}")
def profile_setting(request: Request, profile_id: int, key: str = Form(""), value: str = Form(""),
                    sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            if key not in EDIT_SETTINGS:
                raise profiles.ProfileError("Tego ustawienia nie zmienia się tutaj.")
            profiles.set_profile_setting(conn, profile_id, key, parse_setting(key, value))
        except profiles.ProfileError as e:
            return _profiles_page(request, conn, str(e), 400)
        auth.audit(conn, "profile_setting", user=sess["login"], ip=client_ip(request), target=str(profile_id),
                   params={"key": key, "value": value})
    return RedirectResponse("/admin/profile", 303)
