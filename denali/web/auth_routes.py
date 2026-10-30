"""Logowanie (hasło + 2FA), wylogowanie, konto: hasło, sesje, włączenie 2FA, audit log."""
import hmac
import secrets
from urllib.parse import quote

import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import auth
from ..models import app_user, audit_log
from ..templating import render
from .deps import (COOKIE, PRELOGIN_COOKIE, check_csrf, client_ip, get_session, require_admin, require_user,
                   safe_next, set_cookie)

router = APIRouter()


def _secure(request: Request) -> bool:
    return not request.app.state.settings.dev


def _login_page(request: Request, error: str | None = None, nxt: str = "", status: int = 200):
    pre = secrets.token_urlsafe(24)
    resp = render(request, "login.html", {"pre": pre, "next": nxt, "error": error}, status)
    set_cookie(resp, PRELOGIN_COOKIE, pre, 900, _secure(request))
    return resp


def _signed_in(request: Request, token: str, dest: str, max_age: int = auth.MAX_AGE):
    resp = RedirectResponse(dest, 303)
    set_cookie(resp, COOKIE, token, max_age, _secure(request))
    resp.delete_cookie(PRELOGIN_COOKIE, path="/")
    return resp


@router.get("/login")
def login_form(request: Request, next: str = ""):
    sess = get_session(request)
    if sess and sess["kind"] != "pending":
        return RedirectResponse(safe_next(next) or "/", 303)
    return _login_page(request, nxt=safe_next(next))


@router.post("/login")
def login(request: Request, login: str = Form(""), password: str = Form(""), pre: str = Form(""), next: str = Form("")):
    nxt = safe_next(next)
    cookie = request.cookies.get(PRELOGIN_COOKIE, "")
    if not pre or not cookie or not hmac.compare_digest(pre, cookie):
        return _login_page(request, "Formularz wygasł. Spróbuj jeszcze raz.", nxt, 400)
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    with request.app.state.engine.begin() as conn:
        if auth.login_blocked(conn, login, ip):
            auth.audit(conn, "login_blocked", user=login[:64], ip=ip, result="zablokowane")
            return _login_page(request, "Za dużo nieudanych prób. Spróbuj za 15 minut.", nxt, 429)
        user = auth.get_user(conn, login)
        ok = auth.verify_password(user["pw_hash"] if user else None, password) and user and not user["disabled"]
        if not ok:
            auth.record_attempt(conn, login, ip, False)
            auth.audit(conn, "login_failed", user=login[:64], ip=ip, result="złe hasło")
            return _login_page(request, "Zły login albo hasło.", nxt, 400)
        if user["totp_ct"]:
            # hasło dobre, ale logowanie jeszcze nie — próba liczy się jako udana dopiero po kodzie 2FA
            token = auth.create_session(conn, user["id"], "pending", ip, ua)
            return _signed_in(request, token, "/login/2fa" + (f"?next={quote(nxt)}" if nxt else ""), auth.PENDING_AGE)
        auth.record_attempt(conn, login, ip, True)
        token = auth.create_session(conn, user["id"], "zwykla", ip, ua)
        auth.audit(conn, "login", user=user["login"], ip=ip)
    return _signed_in(request, token, nxt or "/")


@router.get("/login/2fa")
def second_factor_form(request: Request, next: str = ""):
    sess = get_session(request)
    if not sess or sess["kind"] != "pending":
        return RedirectResponse("/login", 303)
    return render(request, "login_2fa.html", {"next": safe_next(next), "error": None})


@router.post("/login/2fa")
async def second_factor(request: Request):
    sess = get_session(request)
    if not sess or sess["kind"] != "pending":
        return RedirectResponse("/login", 303)
    await check_csrf(request, sess["csrf"])
    form = await request.form()
    code, nxt = str(form.get("code", "")), safe_next(str(form.get("next", "")))
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    box = request.app.state.box
    with request.app.state.engine.begin() as conn:
        if auth.login_blocked(conn, sess["login"], ip):
            auth.revoke(conn, sess["token"])
            auth.audit(conn, "login_blocked", user=sess["login"], ip=ip, result="zablokowane")
            resp = _login_page(request, "Za dużo nieudanych prób. Spróbuj za 15 minut.", nxt, 429)
            resp.delete_cookie(COOKIE, path="/")
            return resp
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        method = auth.check_second_factor(conn, user, code, box)
        if not method:
            auth.record_attempt(conn, sess["login"], ip, False)
            auth.audit(conn, "2fa_failed", user=sess["login"], ip=ip, result="zły kod")
            return render(request, "login_2fa.html", {"next": nxt, "error": "Zły albo już użyty kod."}, 400)
        auth.revoke(conn, sess["token"])
        auth.record_attempt(conn, sess["login"], ip, True)
        token = auth.create_session(conn, user["id"], "zwykla", ip, ua)
        auth.audit(conn, "login", user=user["login"], ip=ip, params={"2fa": method})
    return _signed_in(request, token, nxt or "/")


@router.post("/logout")
async def logout(request: Request):
    sess = get_session(request)
    if sess:
        await check_csrf(request, sess["csrf"])
        with request.app.state.engine.begin() as conn:
            auth.revoke(conn, sess["token"])
            auth.audit(conn, "logout", user=sess["login"], ip=client_ip(request))
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ───────────────────────── konto ─────────────────────────
def _account_page(request: Request, sess: dict, message: str | None = None, error: str | None = None, status=200):
    with request.app.state.engine.connect() as conn:
        sessions = auth.user_sessions(conn, sess["user_id"])
    return render(request, "konto.html", {"sessions": sessions, "current": sess["public_id"],
                                          "message": message, "error": error}, status)


@router.get("/konto")
def account(request: Request, sess: dict = Depends(require_user)):
    return _account_page(request, sess)


@router.post("/konto/haslo")
def change_password(request: Request, old: str = Form(""), new: str = Form(""), new2: str = Form(""),
                    sess: dict = Depends(require_user)):
    with request.app.state.engine.begin() as conn:
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        if not auth.verify_password(user["pw_hash"], old):
            auth.audit(conn, "password_change", user=sess["login"], ip=client_ip(request), result="złe obecne hasło")
            return _account_page(request, sess, error="Złe obecne hasło.", status=400)
        if new != new2:
            return _account_page(request, sess, error="Nowe hasła się różnią.", status=400)
        try:
            auth.set_password(conn, sess["user_id"], new, keep_token=sess["token"])
        except auth.AuthError as e:
            return _account_page(request, sess, error=str(e), status=400)
        auth.audit(conn, "password_change", user=sess["login"], ip=client_ip(request))
    return _account_page(request, sess, message="Hasło zmienione. Pozostałe sesje zostały wylogowane.")


@router.post("/konto/wyloguj-wszedzie")
def logout_everywhere(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.begin() as conn:
        auth.revoke_all(conn, sess["user_id"], except_token=sess["token"])
        auth.audit(conn, "logout_all", user=sess["login"], ip=client_ip(request))
    return _account_page(request, sess, message="Wylogowano wszystkie inne sesje.")


@router.post("/konto/sesje/{public_id}/usun")
def revoke_session(request: Request, public_id: str, sess: dict = Depends(require_user)):
    with request.app.state.engine.begin() as conn:
        auth.revoke_public(conn, sess["user_id"], public_id)
        auth.audit(conn, "session_revoke", user=sess["login"], ip=client_ip(request), target=public_id)
    if public_id == sess["public_id"]:
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(COOKIE, path="/")
        return resp
    return _account_page(request, sess, message="Sesja wylogowana.")


@router.get("/konto/2fa")
def totp_setup_form(request: Request, sess: dict = Depends(require_user)):
    if sess["totp_ct"]:
        return render(request, "konto_2fa.html", {"enabled": True})
    box = request.app.state.box
    with request.app.state.engine.begin() as conn:
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        if user["totp_pending_ct"]:
            secret = box.decrypt(user["totp_pending_ct"])
        else:
            secret = auth.totp_new_secret()
            conn.execute(app_user.update().where(app_user.c.id == user["id"]).values(totp_pending_ct=box.encrypt(secret)))
    uri = auth.totp_uri(secret, sess["login"])
    return render(request, "konto_2fa.html", {"enabled": False, "secret": secret,
                                              "qr": segno.make(uri, error="m").svg_data_uri(scale=5, border=2),
                                              "forced": sess["role"] == "admin", "error": None})


@router.post("/konto/2fa")
def totp_enable(request: Request, code: str = Form(""), sess: dict = Depends(require_user)):
    if sess["totp_ct"]:
        return RedirectResponse("/konto/2fa", 303)
    box = request.app.state.box
    with request.app.state.engine.begin() as conn:
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        if not user["totp_pending_ct"]:
            return RedirectResponse("/konto/2fa", 303)
        secret = box.decrypt(user["totp_pending_ct"])
        step = auth.totp_verify(secret, code, 0)
        if step is None:
            auth.audit(conn, "2fa_enable", user=sess["login"], ip=client_ip(request), result="zły kod")
            uri = auth.totp_uri(secret, sess["login"])
            return render(request, "konto_2fa.html", {"enabled": False, "secret": secret,
                                                      "qr": segno.make(uri, error="m").svg_data_uri(scale=5, border=2),
                                                      "forced": sess["role"] == "admin",
                                                      "error": "Zły kod. Sprawdź, czy zegar w telefonie jest dokładny."}, 400)
        codes = auth.enable_totp(conn, user["id"], user["totp_pending_ct"], step)
        auth.revoke_all(conn, user["id"], except_token=sess["token"])
        auth.audit(conn, "2fa_enable", user=sess["login"], ip=client_ip(request))
    return render(request, "kody_zapasowe.html", {"codes": codes})


# ───────────────────────── audyt ─────────────────────────
@router.get("/admin/audyt")
def audit_view(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(select(audit_log).order_by(audit_log.c.id.desc()).limit(300)).mappings().all()
    return render(request, "audyt.html", {"rows": rows})
