"""Logowanie przez Pocket ID (OIDC): start, powrót od dostawcy, połączenie i rozłączenie konta.

Start to zwykły link (GET), nie formularz: CSP „form-action 'self'” zablokowałaby przekierowanie formularza do dostawcy.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from .. import auth, oidc
from ..models import app_user, session
from ..templating import render
from .auth_routes import _account_page, _login_page, _secure, _signed_in
from .deps import COOKIE, NotFound, client_ip, get_session, require_user, safe_next, set_cookie

router = APIRouter()
STATE_COOKIE = "denali_oidc"


def _error(request: Request, text: str, status: int = 400):
    return render(request, "blad.html", {"title": "Pocket ID", "text": text}, status)


def _enabled(request: Request):
    if not request.app.state.settings.oidc_enabled:
        raise NotFound("Logowanie przez Pocket ID nie jest włączone (DENALI_OIDC_ISSUER, DENALI_OIDC_CLIENT_ID).")
    return request.app.state.settings


@router.get("/login/oidc")
def start(request: Request, cel: str = "login", next: str = ""):
    settings = _enabled(request)
    sess, uid = get_session(request), None
    if cel in ("link", "reauth"):
        if not sess or sess["kind"] != "zwykla":
            return RedirectResponse("/login", 303)
        if cel == "link" and not auth.reauth_fresh(sess):
            return RedirectResponse("/potwierdz?next=/konto", 303)
        if cel == "reauth" and not sess["oidc_sub"]:
            return RedirectResponse(f"/potwierdz?next={safe_next(next)}", 303)
        uid = sess["user_id"]
    elif cel != "login":
        raise NotFound()
    try:
        url, state = oidc.begin(settings, request.app.state.box, cel, safe_next(next), uid,
                                getattr(request.app.state, "oidc_transport", None))
    except oidc.OIDCError as e:
        if cel == "login":
            return _login_page(request, str(e), safe_next(next), 502)
        return _error(request, str(e), 502)
    resp = RedirectResponse(url, 303)
    set_cookie(resp, STATE_COOKIE, state, oidc.STATE_TTL, _secure(request))
    return resp


@router.get("/login/oidc/callback")
def callback(request: Request):
    settings = _enabled(request)
    params = dict(request.query_params)
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    box, transport = request.app.state.box, getattr(request.app.state, "oidc_transport", None)
    purpose, nxt = "login", ""
    try:
        st = oidc.read_state(box, request.cookies.get(STATE_COOKIE, ""), params.get("state", ""))
        purpose, nxt = st["purpose"], safe_next(st.get("next"))
        ident = oidc.finish(settings, st, params, transport)
        sess = get_session(request)
        if purpose == "login":
            denied = None
            with request.app.state.engine.begin() as conn:   # odmowa też ma zostać w audycie — bez wycofania transakcji
                try:
                    user = oidc.login_user(conn, settings, ident, ip)
                except oidc.OIDCError as e:
                    denied = e
                else:
                    if sess:
                        auth.revoke(conn, sess["token"])
                    auth.record_attempt(conn, user["login"], ip, True)
                    token = auth.create_session(conn, user["id"], "zwykla", ip, ua, method="oidc")
                    auth.audit(conn, "login", user=user["login"], ip=ip, params={"przez": "pocket-id"})
            if denied:
                raise denied
            resp = _signed_in(request, token, nxt or "/")
        else:
            if not sess or sess["kind"] != "zwykla" or sess["user_id"] != st.get("uid"):
                raise oidc.OIDCError("Sesja Denali się zmieniła w trakcie. Zaloguj się i spróbuj jeszcze raz.")
            with request.app.state.engine.begin() as conn:
                if purpose == "link":
                    oidc.link(conn, sess["user_id"], ident)
                    auth.audit(conn, "oidc_link", user=sess["login"], ip=ip, params={"pocket_id": ident["username"]})
                elif sess["oidc_sub"] != ident["key"]:
                    auth.audit(conn, "reauth_failed", user=sess["login"], ip=ip, result="inne konto Pocket ID")
                else:
                    auth.mark_reauth(conn, sess["token"])
                    auth.audit(conn, "reauth", user=sess["login"], ip=ip, params={"przez": "pocket-id"})
            if purpose == "reauth" and sess["oidc_sub"] != ident["key"]:
                raise oidc.OIDCError("W Pocket ID potwierdziło się inne konto niż to, które jest połączone.")
            resp = RedirectResponse((nxt or "/") if purpose == "reauth" else "/konto", 303)
    except oidc.OIDCError as e:
        resp = _login_page(request, str(e), nxt, 400) if purpose == "login" else _error(request, str(e))
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@router.post("/konto/oidc/rozlacz")
def unlink(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.begin() as conn:
        user = conn.execute(select(app_user).where(app_user.c.id == sess["user_id"])).mappings().first()
        if not auth.has_password(user):
            return _account_page(request, sess, error="To konto nie ma hasła — po rozłączeniu nie dałoby się zalogować.",
                                 status=400)
        oidc.unlink(conn, sess["user_id"])
        # sesje otwarte przez Pocket ID przestają obowiązywać razem z połączeniem (także ta, jeśli tak weszła)
        conn.execute(delete(session).where(session.c.user_id == sess["user_id"], session.c.method == "oidc"))
        auth.audit(conn, "oidc_unlink", user=sess["login"], ip=client_ip(request))
    if sess["method"] == "oidc":
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(COOKIE, path="/")
        return resp
    request.state.sess = {**sess, "oidc_sub": None}
    return _account_page(request, sess, message="Konto rozłączone z Pocket ID.")
