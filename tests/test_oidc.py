import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from denali import oidc
from denali.app import create_app
from denali.config import Settings
from denali.migrate import upgrade
from denali.models import audit_log, session

from .conftest import ADMIN_PW, csrf_from, do_login, make_user, new_client, user_row

ISSUER = "https://id.test"


class FakePocketID:
    """Udawany Pocket ID: discovery, token (ID token bez podpisu — Denali pobiera go bezpośrednio po TLS)."""

    def __init__(self):
        self.codes: dict[str, dict] = {}
        self.token_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                                             "token_endpoint": ISSUER + "/api/oidc/token",
                                             "token_endpoint_auth_methods_supported": ["client_secret_basic"]})
        if request.url.path == "/api/oidc/token":
            self.token_requests.append(request)
            code = parse_qs(request.content.decode())["code"][0]
            claims = self.codes.pop(code, None)
            if claims is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": _jwt(claims), "access_token": "at"})
        return httpx.Response(404)


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.podpis"


@pytest.fixture
def provider():
    oidc._discovery.clear()
    return FakePocketID()


def _app(tmp_path, provider, **kw):
    s = Settings(_env_file=None, data_dir=tmp_path, encryption_key=Fernet.generate_key().decode(), dev=False,
                 **{"base_url": "https://denali.test", "oidc_issuer": ISSUER, "oidc_client_id": "denali",
                    "oidc_client_secret": "tajne", **kw})
    upgrade(s)
    a = create_app(s)
    a.state.oidc_transport = httpx.MockTransport(provider.handler)
    return a


@pytest.fixture
def app(tmp_path, provider):
    a = _app(tmp_path, provider)
    yield a
    a.state.engine.dispose()


@pytest.fixture
def app_groups(tmp_path, provider):
    a = _app(tmp_path, provider, oidc_admin_group="denali-admins", oidc_viewer_group="denali-podglad")
    yield a
    a.state.engine.dispose()


def oidc_round(client, provider, start="/login/oidc", sub="u-1", username="jakub", groups=None, **extra):
    """Klik „Zaloguj przez Pocket ID” → powrót od dostawcy. Zwraca odpowiedź callbacku."""
    r = client.get(start)
    assert r.status_code == 303, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    claims = {"iss": ISSUER, "aud": "denali", "sub": sub, "exp": time.time() + 300, "nonce": q["nonce"][0],
              "preferred_username": username, "groups": groups or [], **extra}
    provider.codes["kod"] = claims
    return client.get(f"/login/oidc/callback?code=kod&state={q['state'][0]}")


def audits(app):
    with app.state.engine.connect() as conn:
        return [r.action for r in conn.execute(select(audit_log.c.action).order_by(audit_log.c.id))]


# ───────────────────────── start ─────────────────────────
def test_przycisk_tylko_gdy_wlaczone(tmp_path, provider):
    off = _app(tmp_path / "off", provider, oidc_issuer="")
    with new_client(off) as c:
        assert "/login/oidc" not in c.get("/login").text
        assert c.get("/login/oidc").status_code == 404
    off.state.engine.dispose()
    on = _app(tmp_path / "on", provider)
    with new_client(on) as c:
        assert 'href="/login/oidc"' in c.get("/login").text
    on.state.engine.dispose()


def test_start_pkce_state_nonce(app, provider):
    with new_client(app) as c:
        r = c.get("/login/oidc")
        url = urlsplit(r.headers["location"])
        q = parse_qs(url.query)
        assert f"{url.scheme}://{url.netloc}{url.path}" == ISSUER + "/authorize"
        assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["denali"]
        assert q["redirect_uri"] == ["https://denali.test/login/oidc/callback"]
        assert "groups" in q["scope"][0] and q["state"][0] and q["nonce"][0]
        cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("denali_oidc=")][0].lower()
        assert "httponly" in cookie and "secure" in cookie


def test_issuer_tylko_https(tmp_path, provider):
    a = _app(tmp_path, provider, oidc_issuer="http://id.test")
    with new_client(a) as c:
        r = c.get("/login/oidc")
        assert r.status_code == 502 and "https://" in r.text
    a.state.engine.dispose()


# ───────────────────────── logowanie ─────────────────────────
def test_niepolaczone_konto_bez_grup_nie_wejdzie(app, provider):
    with new_client(app) as c:
        r = oidc_round(c, provider)
        assert r.status_code == 400 and "nie jest połączone" in r.text
        assert c.get("/").status_code == 303
    assert "oidc_denied" in audits(app)


def test_konto_z_grupy_admin_bez_2fa_denali(app_groups, provider):
    with new_client(app_groups) as c:
        r = oidc_round(c, provider, groups=["denali-admins"])
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/").status_code == 200        # bez wymuszania 2FA Denali — klucz dostępu jest w Pocket ID
        assert "Pocket ID" in c.get("/konto").text
    u = user_row(app_groups, "jakub")
    assert u["role"] == "admin" and u["oidc_sub"] == f"{ISSUER}|u-1"
    with new_client(app_groups) as c:            # hasłem na takie konto się nie wejdzie
        assert do_login(c, "jakub", "cokolwiek-cokolwiek").status_code == 400
    # client_secret w nagłówku Basic, verifier PKCE w żądaniu
    req = provider.token_requests[0]
    assert req.headers["authorization"].startswith("Basic ") and b"code_verifier=" in req.content


def test_poza_grupami_brak_dostepu(app_groups, provider):
    with new_client(app_groups) as c:
        assert "nie należy do grupy" in oidc_round(c, provider, groups=["inna"]).text
    assert user_row(app_groups, "jakub") is None


def test_nazwa_u_dostawcy_nie_przejmuje_konta(app_groups, provider):
    make_user(app_groups, "admin")
    with new_client(app_groups) as c:
        oidc_round(c, provider, username="admin", groups=["denali-podglad"])
    assert user_row(app_groups, "admin")["oidc_sub"] is None
    assert user_row(app_groups, "admin-oidc")["role"] == "podglad"


def test_rola_z_grup_ale_ostatni_admin_zostaje(app_groups, provider):
    with new_client(app_groups) as c:
        oidc_round(c, provider, groups=["denali-admins"])
    with new_client(app_groups) as c:
        assert oidc_round(c, provider, groups=["denali-podglad"]).status_code == 303
    assert user_row(app_groups, "jakub")["role"] == "admin"
    assert "oidc_role_kept" in audits(app_groups)


@pytest.mark.parametrize("bad, text", [({"nonce": "inny"}, "nonce"), ({"aud": "ktos-inny"}, "client ID"),
                                       ({"iss": "https://zly.test"}, "innego dostawcy"),
                                       ({"exp": time.time() - 3600}, "wygasł")])
def test_zly_id_token(app_groups, provider, bad, text):
    with new_client(app_groups) as c:
        r = oidc_round(c, provider, groups=["denali-admins"], **bad)
        assert r.status_code == 400 and text in r.text
    assert user_row(app_groups, "jakub") is None


def test_zly_state(app_groups, provider):
    with new_client(app_groups) as c:
        c.get("/login/oidc")
        r = c.get("/login/oidc/callback?code=kod&state=podrobiony")
        assert r.status_code == 400 and "wygasło" in r.text


# ───────────────────────── połączenie, potwierdzenie, rozłączenie ─────────────────────────
def _password_user(app, client):
    make_user(app, "jan", role="podglad")
    assert do_login(client, "jan").status_code == 303


def _reauth(client, nxt="/konto"):
    page = client.get(f"/potwierdz?next={nxt}").text
    return client.post("/potwierdz", data={"csrf_token": csrf_from(page), "password": ADMIN_PW, "next": nxt})


def test_polaczenie_konta(app, provider):
    with new_client(app) as c:
        _password_user(app, c)
        assert c.get("/login/oidc?cel=link").headers["location"].startswith("/potwierdz")   # najpierw hasło
        _reauth(c)
        r = oidc_round(c, provider, start="/login/oidc?cel=link")
        assert r.status_code == 303 and r.headers["location"] == "/konto"
    assert user_row(app, "jan")["oidc_sub"] == f"{ISSUER}|u-1"
    with new_client(app) as c:                        # teraz wchodzi przez Pocket ID na to samo konto
        assert oidc_round(c, provider).status_code == 303
        assert "jan" in c.get("/").text
    assert "oidc_link" in audits(app)


def test_tozsamosc_polaczona_z_innym_kontem(app, provider):
    with new_client(app) as c:
        _password_user(app, c)
        _reauth(c)
        oidc_round(c, provider, start="/login/oidc?cel=link")
    make_user(app, "ewa", role="podglad")
    with new_client(app) as c:
        do_login(c, "ewa")
        _reauth(c)
        r = oidc_round(c, provider, start="/login/oidc?cel=link")
        assert r.status_code == 400 and "już połączone" in r.text
    assert user_row(app, "ewa")["oidc_sub"] is None


def test_potwierdzenie_przez_pocket_id(app_groups, provider):
    with new_client(app_groups) as c:
        oidc_round(c, provider, groups=["denali-admins"])
        page = c.get("/potwierdz?next=/akcje").text
        assert "Potwierdź w Pocket ID" in page and 'name="password"' not in page
        r = oidc_round(c, provider, start="/login/oidc?cel=reauth&next=/akcje", groups=["denali-admins"],
                       auth_time=time.time() - 3600)   # dostawca nie zapytał ponownie
        assert r.status_code == 400 and "nie poprosił" in r.text
        r = oidc_round(c, provider, start="/login/oidc?cel=reauth&next=/akcje", groups=["denali-admins"],
                       auth_time=time.time())
        assert r.status_code == 303 and r.headers["location"] == "/akcje"
        r = oidc_round(c, provider, start="/login/oidc?cel=reauth", sub="ktos-inny", groups=["denali-admins"],
                       auth_time=time.time())
        assert "inne konto" in r.text
    assert audits(app_groups).count("reauth") == 1


def test_rozlaczenie(app, provider):
    with new_client(app) as c:
        _password_user(app, c)
        _reauth(c)
        oidc_round(c, provider, start="/login/oidc?cel=link")
    with new_client(app) as other:
        oidc_round(other, provider)
        with new_client(app) as c:
            do_login(c, "jan")
            page = c.get("/konto").text
            r = c.post("/konto/oidc/rozlacz", data={"csrf_token": csrf_from(page)})
            assert "rozłączone" in r.text
        assert other.get("/").status_code == 303          # sesja z Pocket ID wylogowana
    assert user_row(app, "jan")["oidc_sub"] is None
    with app.state.engine.connect() as conn:
        assert not conn.execute(select(session).where(session.c.method == "oidc")).first()


def test_konto_bez_hasla_nie_rozlaczy_sie(app_groups, provider):
    with new_client(app_groups) as c:
        oidc_round(c, provider, groups=["denali-podglad"])
        page = c.get("/konto").text
        assert "Rozłącz" not in page and "Zmiana hasła" not in page
        r = c.post("/konto/oidc/rozlacz", data={"csrf_token": csrf_from(page)})
        assert r.status_code == 400
    assert user_row(app_groups, "jakub")["oidc_sub"]
