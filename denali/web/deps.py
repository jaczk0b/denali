"""Wspólne elementy tras: kto jest zalogowany, wymagane role, CSRF, ciasteczka."""
import hmac
from urllib.parse import quote

from fastapi import Request

from .. import auth

COOKIE = "denali_session"
PRELOGIN_COOKIE = "denali_prelogin"
# Administrator bez 2FA może wejść tylko tutaj (włączenie 2FA jest obowiązkowe).
PATHS_WITHOUT_2FA = {"/konto/2fa", "/logout"}


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
    with request.app.state.engine.begin() as conn:
        sess = auth.load_session(conn, token, client_ip(request))
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
    if sess["role"] == "admin" and not sess["totp_ct"] and request.url.path not in PATHS_WITHOUT_2FA:
        raise Redirect("/konto/2fa")
    if request.method not in ("GET", "HEAD"):
        await check_csrf(request, sess["csrf"])
    return sess


async def require_admin(request: Request) -> dict:
    sess = await require_user(request)
    if sess["role"] != "admin":
        raise Forbidden()
    return sess


def set_cookie(resp, name: str, value: str, max_age: int, secure: bool) -> None:
    # Lax, nie Strict: link z powiadomienia (Discord) ma otwierać panel bez ponownego logowania.
    # Zapisy chroni token CSRF w każdym formularzu.
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/")
