"""Ponowne potwierdzenie hasłem (skarbiec), parowanie tabletu (kiosk), profile."""
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from .. import auth, profiles
from ..db import kv_get, kv_set
from ..models import app_user, device, profile, session
from ..templating import render
from .auth_routes import _secure
from .deps import COOKIE, PRELOGIN_COOKIE, client_ip, get_session, require_admin, require_user, safe_next, set_cookie
from .infra_routes import EDIT_SETTINGS, parse_setting

router = APIRouter()
KIOSK_LOGIN = "tablet"
PAIR_TTL = 600                                      # kod parowania ważny 10 minut
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # bez 0/O i 1/I/L


def _hash(code):
    return hashlib.sha256(code.strip().upper().replace("-", "").replace(" ", "").encode()).hexdigest()


# ───────────────────────── ponowne potwierdzenie ─────────────────────────
@router.get("/potwierdz")
def reauth_form(request: Request, next: str = "", sess: dict = Depends(require_user)):
    return render(request, "potwierdz.html", {"next": safe_next(next), "error": None})


@router.post("/potwierdz")
def reauth(request: Request, password: str = Form(""), code: str = Form(""), next: str = Form(""),
           sess: dict = Depends(require_user)):
    nxt, ip = safe_next(next), client_ip(request)
    with request.app.state.engine.begin() as conn:
        if auth.login_blocked(conn, sess["login"], ip):
            return render(request, "potwierdz.html", {"next": nxt, "error": "Za dużo nieudanych prób. "
                                                                            "Spróbuj za 15 minut."}, 429)
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        ok = auth.verify_password(user["pw_hash"], password)
        if ok and user["totp_ct"]:
            ok = bool(auth.check_second_factor(conn, user, code, request.app.state.box))
        if not ok:
            auth.record_attempt(conn, sess["login"], ip, False)
            auth.audit(conn, "reauth_failed", user=sess["login"], ip=ip, result="złe dane")
            return render(request, "potwierdz.html", {"next": nxt, "error": "Złe hasło albo kod."}, 400)
        auth.mark_reauth(conn, sess["token"])
        auth.audit(conn, "reauth", user=sess["login"], ip=ip)
    return RedirectResponse(nxt or "/", 303)


# ───────────────────────── tablet (kiosk) ─────────────────────────
def _kiosk_user(conn):
    u = auth.get_user(conn, KIOSK_LOGIN)
    if u:
        if u["role"] != "podglad":
            raise RuntimeError(f"Konto „{KIOSK_LOGIN}” istnieje i nie jest kontem podglądu — tablet go nie użyje.")
        return u["id"]
    # konto tylko do podglądu, z losowym hasłem, którego nikt nie zna — wejście wyłącznie kodem parowania
    return auth.create_user(conn, KIOSK_LOGIN, secrets.token_urlsafe(32), "podglad")


def _tablet_admin_page(request, conn, code=None):
    kiosks = conn.execute(select(session.c.public_id, session.c.ip, session.c.user_agent, session.c.created_at,
                                 session.c.last_seen_at, session.c.expires_at)
                          .where(session.c.kind == "kiosk").order_by(session.c.created_at)).mappings().all()
    pending = kv_get(conn, "kiosk.pair")
    active = pending and pending.get("expires", 0) > time.time()
    return render(request, "admin_tablet.html", {"code": code, "kiosks": kiosks,
                                                 "pending_until": pending["expires"] if active else None})


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
    return render(request, "admin_profile.html", {"rows": rows, "usage": usage, "defs": profiles.SETTINGS,
                                                  "edit_settings": EDIT_SETTINGS, "error": error}, status_code)


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
