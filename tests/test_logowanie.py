import re
import time

import pytest
from sqlalchemy import select

from denali import auth
from denali.models import audit_log

from .conftest import ADMIN_PW, csrf_from, do_login, enable_2fa, make_user, new_client, user_row


def actions(app):
    with app.state.engine.connect() as conn:
        return [r.action for r in conn.execute(select(audit_log.c.action).order_by(audit_log.c.id))]


# ───────────────────────── hasła i TOTP (bez WWW) ─────────────────────────
def test_haslo_za_krotkie():
    with pytest.raises(auth.AuthError):
        auth.hash_password("krotkie")


def test_haslo_argon2id_i_weryfikacja():
    h = auth.hash_password("poprawne-dlugie-haslo")
    assert h.startswith("$argon2id$")
    assert auth.verify_password(h, "poprawne-dlugie-haslo")
    assert not auth.verify_password(h, "zle-haslo-zle-haslo")
    assert not auth.verify_password(None, "cokolwiek-cokolwiek")   # nieistniejące konto


def test_totp_zgodny_z_rfc6238():
    # wektor testowy z RFC 6238 (SHA1, sekret "12345678901234567890", T=59 s) — ostatnie 6 cyfr: 287082
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp_code(secret, 59 // 30) == "287082"


def test_totp_nie_dziala_drugi_raz():
    secret = auth.totp_new_secret()
    now = time.time()
    step = int(now // 30)
    code = auth.totp_code(secret, step)
    assert auth.totp_verify(secret, code, 0, now) == step
    assert auth.totp_verify(secret, code, step, now) is None


# ───────────────────────── logowanie ─────────────────────────
def test_niezalogowany_trafia_na_logowanie(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_zle_haslo(client, app):
    make_user(app)
    r = do_login(client, password="zle-haslo-zle-haslo")
    assert r.status_code == 400 and "Zły login albo hasło" in r.text
    r = do_login(client, login="nie-ma-takiego")
    assert "Zły login albo hasło" in r.text   # ten sam komunikat — nie zdradzamy, czy konto istnieje
    assert actions(app).count("login_failed") == 2


def test_logowanie_bez_formularza_z_naszej_strony(client, app):
    make_user(app)
    r = client.post("/login", data={"login": "admin", "password": ADMIN_PW, "pre": "podrobiony"})
    assert r.status_code == 400 and "Formularz wygasł" in r.text


def test_ciasteczko_sesji_bezpieczne(client, app):
    make_user(app)
    r = do_login(client)
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("denali_session=")][0].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_admin_bez_2fa_musi_je_wlaczyc(client, app):
    make_user(app)
    r = do_login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    for path in ("/", "/konto", "/admin/audyt"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/konto/2fa"
    assert client.get("/konto/2fa").status_code == 200


def test_wlaczenie_2fa_i_logowanie_z_kodem(client, app):
    make_user(app)
    do_login(client)
    secret = enable_2fa(client, app)
    assert client.get("/").status_code == 200
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/konto").text)})

    r = do_login(client)
    assert r.headers["location"] == "/login/2fa"
    assert client.get("/").status_code == 303          # po samym haśle nie ma dostępu
    page = client.get("/login/2fa").text
    step = user_row(app)["totp_last_step"]
    # ten sam kod, którym włączono 2FA, nie zadziała drugi raz
    r = client.post("/login/2fa", data={"csrf_token": csrf_from(page), "code": auth.totp_code(secret, step)})
    assert r.status_code == 400 and "już użyty" in r.text
    r = client.post("/login/2fa", data={"csrf_token": csrf_from(page), "code": auth.totp_code(secret, step + 1)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200
    assert "2fa_failed" in actions(app) and actions(app).count("login") == 2


def test_kod_zapasowy_dziala_raz(client, app):
    make_user(app)
    do_login(client)
    page = client.get("/konto/2fa").text
    secret = re.search(r'Wpisz ręcznie: <span class="mono">([A-Z2-7]+)</span>', page).group(1)
    r = client.post("/konto/2fa", data={"csrf_token": csrf_from(page),
                                         "code": auth.totp_code(secret, int(time.time() // 30))})
    backup = re.findall(r"<li>([0-9a-f]{6}-[0-9a-f]{6})</li>", r.text)
    assert len(backup) == 10

    with new_client(app) as other:
        do_login(other)
        csrf = csrf_from(other.get("/login/2fa").text)
        assert other.post("/login/2fa", data={"csrf_token": csrf, "code": backup[0]}).status_code == 303
    with new_client(app) as third:
        do_login(third)
        csrf = csrf_from(third.get("/login/2fa").text)
        assert third.post("/login/2fa", data={"csrf_token": csrf, "code": backup[0]}).status_code == 400


def test_limit_prob_logowania(client, app):
    make_user(app)
    for _ in range(auth.MAX_FAILS_PER_LOGIN):
        do_login(client, password="zle-haslo-zle-haslo")
    r = do_login(client)                                # dobre hasło, ale za dużo prób
    assert r.status_code == 429 and "Za dużo" in r.text
    assert "login_blocked" in actions(app)


def test_limit_prob_obejmuje_kod_2fa(client, app):
    make_user(app)
    do_login(client)
    enable_2fa(client, app)
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/konto").text)})
    do_login(client)
    for _ in range(auth.MAX_FAILS_PER_LOGIN):
        csrf = csrf_from(client.get("/login/2fa").text)
        client.post("/login/2fa", data={"csrf_token": csrf, "code": "000000"})
    csrf = csrf_from(client.get("/login/2fa").text)
    r = client.post("/login/2fa", data={"csrf_token": csrf, "code": "123456"})
    assert r.status_code == 429


def test_zapis_bez_tokenu_csrf_odrzucony(client, app):
    make_user(app, role="podglad", login="widz")
    do_login(client, login="widz")
    r = client.post("/konto/wyloguj-wszedzie", data={})
    assert r.status_code == 400 and "Formularz wygasł" in r.text


def test_podglad_nie_widzi_audytu(client, app):
    make_user(app, role="podglad", login="widz")
    do_login(client, login="widz")
    assert client.get("/").status_code == 200        # podgląd nie musi mieć 2FA
    assert client.get("/admin/audyt").status_code == 403


# ───────────────────────── sesje ─────────────────────────
def _logged_viewer(app):
    c = new_client(app)
    c.__enter__()
    do_login(c, login="widz")
    return c


def test_zmiana_hasla_wylogowuje_inne_sesje(app):
    make_user(app, role="podglad", login="widz")
    a, b = _logged_viewer(app), _logged_viewer(app)
    csrf = csrf_from(a.get("/konto").text)
    r = a.post("/konto/haslo", data={"csrf_token": csrf, "old": ADMIN_PW, "new": "nowe-dlugie-haslo-1",
                                     "new2": "nowe-dlugie-haslo-1"})
    assert r.status_code == 200 and "Hasło zmienione" in r.text
    assert a.get("/").status_code == 200
    assert b.get("/").status_code == 303


def test_wyloguj_wszedzie(app):
    make_user(app, role="podglad", login="widz")
    a, b = _logged_viewer(app), _logged_viewer(app)
    a.post("/konto/wyloguj-wszedzie", data={"csrf_token": csrf_from(a.get("/konto").text)})
    assert a.get("/").status_code == 200
    assert b.get("/").status_code == 303
    assert "logout_all" in actions(app)


def test_sesja_wygasa_po_bezczynnosci(app):
    uid = make_user(app, role="podglad", login="widz")
    with app.state.engine.begin() as conn:
        token = auth.create_session(conn, uid, "zwykla", now=1000.0)
        assert auth.load_session(conn, token, now=1000.0 + auth.IDLE_TIMEOUT - 1)
        token = auth.create_session(conn, uid, "zwykla", now=1000.0)
        assert auth.load_session(conn, token, now=1000.0 + auth.IDLE_TIMEOUT + 1) is None


def test_sesja_kiosku_tylko_z_adresu_tabletu(app):
    uid = make_user(app, role="podglad", login="tablet")
    with app.state.engine.begin() as conn:
        token = auth.create_session(conn, uid, "kiosk", ip="192.168.1.50")
        assert auth.load_session(conn, token, ip="192.168.1.50")
        assert auth.load_session(conn, token, ip="192.168.1.99") is None


def test_wylaczone_konto_traci_dostep(app):
    uid = make_user(app, role="podglad", login="widz")
    c = _logged_viewer(app)
    with app.state.engine.begin() as conn:
        conn.execute(auth.app_user.update().where(auth.app_user.c.id == uid).values(disabled=True))
    assert c.get("/").status_code == 303


# ───────────────────────── konsola ─────────────────────────
def test_reset_hasla_z_konsoli(app, settings, monkeypatch):
    from denali import __main__ as cli
    from denali.config import get_settings
    make_user(app)
    c = new_client(app)
    c.__enter__()
    do_login(c)
    enable_2fa(c, app)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p="": "haslo-z-konsoli-123")
    monkeypatch.setenv("DENALI_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DENALI_ENCRYPTION_KEY", settings.encryption_key)
    get_settings.cache_clear()
    cli.main(["reset-password", "admin", "--disable-2fa"])
    get_settings.cache_clear()
    assert c.get("/konto").status_code == 303            # stara sesja wylogowana
    assert user_row(app)["totp_ct"] is None
    assert "password_reset" in actions(app)
