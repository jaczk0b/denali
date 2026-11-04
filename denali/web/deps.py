"""Wspólne elementy tras: kto jest zalogowany, wymagane role, CSRF, ciasteczka."""
import hmac
import json
import time
from urllib.parse import quote

from fastapi import Request
from sqlalchemy import delete

from .. import auth
from ..db import kv_get, kv_set
from ..models import setting

COOKIE = "denali_session"
PRELOGIN_COOKIE = "denali_prelogin"
# Administrator bez 2FA może wejść tylko tutaj (włączenie 2FA jest obowiązkowe).
PATHS_WITHOUT_2FA = {"/konto/2fa", "/logout"}
# Odświeżanie na żywo: strumień SSE i pobrania strony w tle (nagłówek) to nie aktywność użytkownika.
LIVE_PATH = "/na-zywo"
LIVE_HEADER = "X-Denali-Live"
RESUME_PATH = "/potwierdz/dokoncz"
PENDING_VALID = 600     # tyle czeka zapamiętane działanie na hasło


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class Forbidden(Exception):
    pass


class CsrfError(Exception):
    pass


class NotFound(Exception):
    def __init__(self, text: str = "Nie ma takiej strony."):
        self.text = text


def client_ip(request: Request) -> str:
    """Prawdziwy adres klienta — uvicorn podstawia go z X-Forwarded-For tylko od zaufanego proxy (DENALI_TRUSTED_PROXIES)."""
    return request.client.host if request.client else ""


def safe_next(nxt: str | None) -> str:
    nxt = nxt or ""
    return nxt if nxt.startswith("/") and not nxt.startswith(("//", "/\\")) else ""


def get_session(request: Request) -> dict | None:
    if hasattr(request.state, "sess"):
        return request.state.sess
    token = request.cookies.get(COOKIE, "")
    touch = request.url.path != LIVE_PATH and request.headers.get(LIVE_HEADER) != "1"
    with request.app.state.engine.begin() as conn:
        sess = auth.load_session(conn, token, client_ip(request), touch=touch)
    if sess:
        sess["token"] = token
    request.state.sess = sess
    return sess


async def check_csrf(request: Request, expected: str) -> None:
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token", "")
    if not sent or not expected or not hmac.compare_digest(str(sent), expected):
        raise CsrfError()


async def require_user(request: Request) -> dict:
    sess = get_session(request)
    if not sess or sess["kind"] == "pending":
        raise Redirect(f"/login?next={quote(request.url.path)}")
    # Po zalogowaniu przez Pocket ID drugim składnikiem jest klucz dostępu u dostawcy — 2FA Denali nie jest wymagane.
    if (sess["role"] == "admin" and not sess["totp_ct"] and sess["method"] != "oidc"
            and request.url.path not in PATHS_WITHOUT_2FA):
        raise Redirect("/konto/2fa")
    if request.method not in ("GET", "HEAD"):
        await check_csrf(request, sess["csrf"])
    return sess


async def require_admin(request: Request) -> dict:
    sess = await require_user(request)
    if sess["role"] != "admin":
        raise Forbidden()
    return sess


def _pending_key(sess: dict) -> str:
    return f"reauth.pending.{sess['user_id']}"


async def require_fresh(request: Request, sess: dict) -> None:
    """Tu potrzeba hasła: pokazuje okno potwierdzenia, a po nim samo dokańcza to działanie (ten sam POST).

    Formularz czeka zaszyfrowany, bo bywa w nim sekret. Działa też z Pocket ID, które wraca do panelu przekierowaniem."""
    if auth.reauth_fresh(sess):
        return
    form = await request.form()
    fields = [(k, v) for k, v in form.multi_items() if k != "csrf_token" and isinstance(v, str)]
    pending = {"session": sess["public_id"], "at": time.time(), "url": request.url.path, "fields": fields}
    with request.app.state.engine.begin() as conn:
        kv_set(conn, _pending_key(sess), request.app.state.box.encrypt(json.dumps(pending)))
    raise Redirect(f"/potwierdz?next={RESUME_PATH}")


def take_pending(conn, box, sess: dict, now: float | None = None) -> dict | None:
    """Zapamiętane działanie tej sesji — jednorazowo."""
    raw = kv_get(conn, _pending_key(sess))
    if not raw:
        return None
    conn.execute(delete(setting).where(setting.c.key == _pending_key(sess)))
    pending = json.loads(box.decrypt(raw))
    fresh = (now or time.time()) - pending["at"] < PENDING_VALID
    return pending if fresh and pending["session"] == sess["public_id"] else None


def set_cookie(resp, name: str, value: str, max_age: int, secure: bool) -> None:
    # Lax, nie Strict: link z powiadomienia (Discord) ma otwierać panel bez ponownego logowania.
    # Zapisy chroni token CSRF w każdym formularzu.
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/")
