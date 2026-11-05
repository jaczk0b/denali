"""Logowanie przez OpenID Connect (Pocket ID; działa też z Authentikiem, Authelią, Keycloakiem).

Przepływ: authorization code + PKCE (S256), state i nonce w zaszyfrowanym, krótkim ciasteczku. Kod wymieniamy na tokeny
bezpośrednio z serwerem dostawcy po zweryfikowanym TLS — wtedy (OIDC Core 3.1.3.7) nie trzeba sprawdzać podpisu
ID tokena; sprawdzamy issuer, audience, ważność i nonce.

Bezpieczeństwo kont (przeniesione z HLC):
- konto Denali łączy się z tożsamością wyłącznie przez „issuer|sub” — nigdy po nazwie (ktoś z loginem „admin”
  u dostawcy nie przejmie lokalnego admina);
- swoje istniejące konto łączysz sam, z zalogowanej sesji, po ponownym potwierdzeniu hasłem;
- rola z grup u dostawcy (gdy grupy są ustawione); synchronizacja nigdy nie odbierze roli ostatniemu administratorowi;
- własne logowanie zostaje zawsze (awaryjne, gdy Pocket ID leży).

Bez zależności od warstwy WWW — trasy są w web/oidc_routes.py.
"""
import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import func, insert, select, update

from . import auth
from .models import app_user

STATE_TTL = 600          # od kliknięcia „Zaloguj przez…” do powrotu od dostawcy
DISCOVERY_TTL = 3600
SCOPES = "openid profile email groups"
NO_PASSWORD = "!oidc"    # pw_hash konta założonego przez dostawcę — hasłem się na nie nie zaloguje
PURPOSES = ("login", "link", "reauth")

_discovery: dict[str, tuple[float, dict]] = {}


class OIDCError(Exception):
    pass


def _http(transport=None) -> httpx.Client:
    return httpx.Client(timeout=10, follow_redirects=False, transport=transport,
                        headers={"Accept": "application/json", "User-Agent": "Denali"})


def _https(url: str) -> bool:
    return urlsplit(url or "").scheme == "https"


def _json(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error_description") or resp.json().get("error") or ""
        except ValueError:
            pass
        raise OIDCError(f"Pocket ID odpowiedział błędem {resp.status_code}. {detail}".strip())
    try:
        data = resp.json()
    except ValueError:
        raise OIDCError("Pocket ID nie odpowiedział JSON-em — sprawdź adres (DENALI_OIDC_ISSUER).") from None
    if not isinstance(data, dict):
        raise OIDCError("Nieoczekiwana odpowiedź Pocket ID.")
    return data


def discovery(issuer: str, transport=None, fresh: bool = False) -> dict:
    issuer = issuer.strip().rstrip("/")
    hit = _discovery.get(issuer)
    if hit and not fresh and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    if not _https(issuer):
        raise OIDCError("Adres Pocket ID (DENALI_OIDC_ISSUER) musi zaczynać się od https://.")
    try:
        with _http(transport) as c:
            doc = _json(c.get(issuer + "/.well-known/openid-configuration"))
    except httpx.HTTPError as e:
        raise OIDCError(f"Nie mogę połączyć się z Pocket ID: {e}") from None
    if (doc.get("issuer") or "").rstrip("/") != issuer:
        raise OIDCError(f"Adres Pocket ID się nie zgadza: dostawca podaje issuer „{doc.get('issuer')}”. "
                        "Wpisz dokładnie ten adres w DENALI_OIDC_ISSUER.")
    for k in ("authorization_endpoint", "token_endpoint"):
        if not _https(doc.get(k)):
            raise OIDCError(f"Brak {k} (https) w konfiguracji Pocket ID.")
    _discovery[issuer] = (time.time(), doc)
    return doc


def redirect_uri(settings) -> str:
    return settings.base_url.rstrip("/") + "/login/oidc/callback"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def begin(settings, box, purpose: str = "login", nxt: str = "", uid: int | None = None,
          transport=None) -> tuple[str, str]:
    """(adres u dostawcy, zaszyfrowany stan do ciasteczka). purpose: login | link (połącz konto) | reauth."""
    if purpose not in PURPOSES:
        raise OIDCError("Nieznany cel logowania.")
    d = discovery(settings.oidc_issuer, transport)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    st = {"state": state, "nonce": nonce, "verifier": verifier, "purpose": purpose, "next": nxt, "uid": uid,
          "ts": time.time()}
    q = {"response_type": "code", "client_id": settings.oidc_client_id, "redirect_uri": redirect_uri(settings),
         "scope": SCOPES, "state": state, "nonce": nonce,
         "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    if purpose == "reauth":
        q.update(prompt="login", max_age="0")   # dostawca ma zapytać o klucz dostępu jeszcze raz
    url = d["authorization_endpoint"]
    return url + ("&" if "?" in url else "?") + urlencode(q), box.encrypt(json.dumps(st))


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise OIDCError("Nieprawidłowy ID token od Pocket ID.") from None
    if not isinstance(data, dict):
        raise OIDCError("Nieprawidłowy ID token od Pocket ID.")
    return data


def read_state(box, cookie: str, state: str) -> dict:
    try:
        st = json.loads(box.decrypt(cookie or ""))
    except ValueError:
        st = None
    if not st or not state or not secrets.compare_digest(str(state), st.get("state", "")):
        raise OIDCError("Logowanie wygasło albo zaczęło się w innej karcie. Spróbuj jeszcze raz.")
    if time.time() - st["ts"] > STATE_TTL:
        raise OIDCError("Logowanie trwało za długo (ponad 10 minut). Spróbuj jeszcze raz.")
    return st


def finish(settings, st: dict, params: dict, transport=None, now: float | None = None) -> dict:
    """Callback: wymienia kod na tokeny i sprawdza ID token. Zwraca tożsamość: key, username, email, groups."""
    if params.get("error"):
        raise OIDCError(f"Pocket ID odmówił: {params.get('error_description') or params.get('error')}")
    if not params.get("code"):
        raise OIDCError("Brak kodu logowania od Pocket ID.")
    d = discovery(settings.oidc_issuer, transport)
    data = {"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri(settings),
            "code_verifier": st["verifier"]}
    auth_ = None
    methods = d.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if settings.oidc_client_secret and "client_secret_basic" in methods:
        auth_ = (settings.oidc_client_id, settings.oidc_client_secret)
    else:
        data["client_id"] = settings.oidc_client_id
        if settings.oidc_client_secret:
            data["client_secret"] = settings.oidc_client_secret
    try:
        with _http(transport) as c:
            tok = _json(c.post(d["token_endpoint"], data=data, auth=auth_))
            cl = _claims(tok.get("id_token") or "")
            info = {}
            if "groups" not in cl and d.get("userinfo_endpoint") and tok.get("access_token") \
                    and _https(d["userinfo_endpoint"]):
                try:
                    info = _json(c.get(d["userinfo_endpoint"],
                                       headers={"Authorization": f"Bearer {tok['access_token']}"}))
                except OIDCError:
                    info = {}
    except httpx.HTTPError as e:
        raise OIDCError(f"Nie mogę połączyć się z Pocket ID: {e}") from None
    now = now if now is not None else time.time()
    aud = cl.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if (cl.get("iss") or "").rstrip("/") != d["issuer"].rstrip("/"):
        raise OIDCError("ID token pochodzi od innego dostawcy.")
    if settings.oidc_client_id not in auds or (len(auds) > 1 and cl.get("azp") not in (None, settings.oidc_client_id)):
        raise OIDCError("ID token nie jest przeznaczony dla Denali (inny client ID).")
    if not isinstance(cl.get("exp"), (int, float)) or cl["exp"] < now - 60:
        raise OIDCError("ID token wygasł — sprawdź zegar na serwerze Denali.")
    if not cl.get("nonce") or not secrets.compare_digest(str(cl["nonce"]), st["nonce"]):
        raise OIDCError("Nieprawidłowy nonce w ID tokenie.")
    if not cl.get("sub"):
        raise OIDCError("Brak identyfikatora użytkownika (sub) w ID tokenie.")
    if st["purpose"] == "reauth":
        at = cl.get("auth_time")
        if not isinstance(at, (int, float)) or at < st["ts"] - 30:
            raise OIDCError("Pocket ID nie poprosił o ponowne zalogowanie — potwierdź hasłem Denali.")
    if info.get("sub") not in (None, cl["sub"]):
        info = {}   # odpowiedź userinfo dla kogoś innego — pomijamy
    groups = cl.get("groups", info.get("groups")) or []
    username = (cl.get("preferred_username") or info.get("preferred_username")
                or (cl.get("email") or info.get("email") or "").split("@")[0])
    return {"key": f"{d['issuer'].rstrip('/')}|{cl['sub']}", "username": str(username or ""),
            "email": str(cl.get("email") or info.get("email") or ""),
            "groups": [str(g) for g in groups] if isinstance(groups, list) else []}


# ───────────────────────── konta ─────────────────────────
def groups_set(settings) -> bool:
    return bool(settings.oidc_admin_group.strip() or settings.oidc_viewer_group.strip() or settings.oidc_user_group.strip())


def role_for(settings, groups) -> str | None:
    have = {g.lower() for g in groups}
    for role, gname in (("admin", settings.oidc_admin_group), ("podglad", settings.oidc_viewer_group),
                        (auth.USER_ROLE, settings.oidc_user_group)):
        if gname.strip() and gname.strip().lower() in have:
            return role
    return None


def _free_login(conn, name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:50] or "uzytkownik"
    base = base if len(base) >= 2 else f"{base}-oidc"
    for cand in [base] + [f"{base}-oidc{i or ''}" for i in range(100)]:
        if not auth.get_user(conn, cand):
            return cand
    raise OIDCError("Nie mogę dobrać wolnego loginu.")


def _other_admins(conn, uid: int) -> int:
    return conn.execute(select(func.count()).select_from(app_user)
                        .where(app_user.c.role == "admin", app_user.c.id != uid, app_user.c.disabled.is_(False))).scalar()


def login_user(conn, settings, ident: dict, ip: str = ""):
    """Konto Denali dla tożsamości z Pocket ID: powiązane (rola zsynchronizowana z grupami) albo nowe (z grup)."""
    who = ident["username"] or ident["key"][-40:]
    u = conn.execute(select(app_user).where(app_user.c.oidc_sub == ident["key"])).mappings().first()
    role = role_for(settings, ident["groups"]) if groups_set(settings) else None
    if u is None:
        if role is None:
            auth.audit(conn, "oidc_denied", user=who[:64], ip=ip, result="konto niepołączone",
                       params={"groups": ident["groups"][:20]})
            if groups_set(settings):
                raise OIDCError("Twoje konto w Pocket ID nie należy do grupy z dostępem do Denali.")
            raise OIDCError("To konto Pocket ID nie jest połączone z Denali. Zaloguj się hasłem i połącz je w "
                            "Konto → Pocket ID.")
        now = time.time()
        login = _free_login(conn, ident["username"])
        uid = conn.execute(insert(app_user).values(login=login, pw_hash=NO_PASSWORD, role=role, totp_last_step=0,
                                                   backup_codes=[], disabled=False, created_at=now, pw_changed_at=now,
                                                   oidc_sub=ident["key"])).inserted_primary_key[0]
        auth.audit(conn, "oidc_user_create", user=login, ip=ip, target=login, params={"role": role})
        return conn.execute(select(app_user).where(app_user.c.id == uid)).mappings().first()
    if u["disabled"]:
        auth.audit(conn, "oidc_denied", user=u["login"], ip=ip, result="konto wyłączone")
        raise OIDCError("To konto Denali jest wyłączone.")
    if groups_set(settings):
        if role is None:
            auth.audit(conn, "oidc_denied", user=u["login"], ip=ip, result="poza grupami",
                       params={"groups": ident["groups"][:20]})
            raise OIDCError("Twoje konto w Pocket ID nie należy już do grupy z dostępem do Denali.")
        if role != u["role"]:
            if u["role"] == "admin" and _other_admins(conn, u["id"]) == 0:
                auth.audit(conn, "oidc_role_kept", user=u["login"], ip=ip, params={"chciane": role})
            else:
                conn.execute(update(app_user).where(app_user.c.id == u["id"]).values(role=role))
                auth.audit(conn, "oidc_role_change", user=u["login"], ip=ip, params={"z": u["role"], "na": role})
                u = conn.execute(select(app_user).where(app_user.c.id == u["id"])).mappings().first()
    return u


def link(conn, uid: int, ident: dict) -> None:
    other = conn.execute(select(app_user.c.login).where(app_user.c.oidc_sub == ident["key"], app_user.c.id != uid)).scalar()
    if other:
        raise OIDCError(f"To konto Pocket ID jest już połączone z kontem Denali „{other}”.")
    conn.execute(update(app_user).where(app_user.c.id == uid).values(oidc_sub=ident["key"]))


def unlink(conn, uid: int) -> None:
    conn.execute(update(app_user).where(app_user.c.id == uid).values(oidc_sub=None))
