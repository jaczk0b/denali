"""Konto „uzytkownik”: widzi tylko przydzielone usługi, a reszta panelu jest zamknięta także dla nowych tras."""
import importlib
import pkgutil
import re

import pytest
from sqlalchemy import select

from denali import auth, incidents, inventory as inv, maintenance, oidc, shares, web
from denali.config import Settings
from denali.models import app_user, share

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

PW = "dlugie-haslo-uzytkownika"
SECRET = "pve-tajny-host"
# Trasy bez sesji: logowanie, zdrowie, token metryk, motyw (ciasteczko), parowanie tabletu kodem jednorazowym,
# skrypt agenta i jego rejestracja kodem.
PUBLIC = {"/login", "/login/2fa", "/login/oidc", "/login/oidc/callback", "/logout", "/health", "/motyw", "/metrics",
          "/api/stan", "/tablet", "/agent/denali-agent.py", "/agent/denali-agent.py.sha256", "/api/agent/v1/rejestracja"}


@pytest.fixture
def lab(app):
    with app.state.engine.begin() as conn:
        host = inv.create_device(conn, SECRET, "host")
        inv.add_address(conn, host, "ip", "192.168.77.5")
        immich = shares.create(conn, "Immich", "link", "https://zdjecia.example.com", device_id=host)
        smb = shares.create(conn, "Dysk", "adres", "\\\\nas\\zdjecia", body_md="Otwórz Eksplorator.")
        hidden = shares.create(conn, "Tylko dla innych", "link", "https://inne.example.com")
        uid = auth.create_user(conn, "ania", PW, shares.ROLE)
        guide = conn.execute(select(share.c.id).where(share.c.title == "Tailscale")).scalar()
        shares.assign(conn, uid, [immich, smb, guide])
    return {"uid": uid, "host": host, "immich": immich, "smb": smb, "hidden": hidden, "guide": guide}


@pytest.fixture
def user(app, lab):
    with new_client(app) as c:
        do_login(c, login="ania", password=PW)
        yield c


@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


def all_routes():
    for mod in pkgutil.iter_modules(web.__path__):
        router = getattr(importlib.import_module(f"denali.web.{mod.name}"), "router", None)
        yield from (router.routes if router else [])


def test_wejscie_trafia_na_moje_uslugi(user):
    r = user.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/moje"


def test_widzi_tylko_przydzielone(user, lab):
    page = user.get("/moje").text
    assert "Immich" in page and "https://zdjecia.example.com" in page
    assert "\\\\nas\\zdjecia" in page and "Kopiuj adres" in page and "Tailscale" in page
    assert "Tylko dla innych" not in page
    assert SECRET not in page and "/infrastruktura" not in page and "/urzadzenia" not in page
    assert user.get(f"/moje/{lab['guide']}").status_code == 200
    assert user.get(f"/moje/{lab['hidden']}").status_code == 404


def test_cudza_instrukcja_niedostepna(app, user):
    with app.state.engine.begin() as conn:
        other = shares.create(conn, "Sekret", "instrukcja", body_md="tajne")
    assert user.get(f"/moje/{other}").status_code == 404


def test_cala_reszta_panelu_jest_zamknieta(app, user):
    """Przechodzi po wszystkich trasach, więc obejmie też te dopisane w przyszłości."""
    token = csrf_from(user.get("/konto").text)
    checked, bad = 0, []
    for route in all_routes():
        if route.path in PUBLIC or route.path == "/" or not hasattr(route, "methods"):
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        if path == "/moje" or path.startswith(("/moje/", "/konto", "/potwierdz")):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = user.request(method, path, data={"csrf_token": token} if method != "GET" else None)
            if r.status_code not in (401, 403) or SECRET in r.text:      # 401: trasy agenta i ich własny token
                bad.append((method, path, r.status_code))
            checked += 1
    assert not bad
    assert checked > 80


def test_brak_licznikow_i_narzedzi_w_ukladzie_strony(app, user, lab):
    with app.state.engine.begin() as conn:
        g = inv.load_graph(conn)
        [f] = g.analyze({lab["host"]: "down"})
        incidents.record_finding(conn, g, f)
    page = user.get("/moje").text
    assert 'class="badge' not in page and "/szukaj" not in page and "Incydenty" not in page
    assert user.get("/szukaj/szybko?q=pve").status_code == 403


def test_status_bez_nazw_i_powodow(app, user, lab):
    with app.state.engine.begin() as conn:
        g = inv.load_graph(conn)
        [f] = g.analyze({lab["host"]: "down"})
        incidents.record_finding(conn, g, f)
    page = user.get("/moje/status").text
    assert "Coś nie działa" in page and "Immich" in page and "Nie działa" in page
    assert SECRET not in page and "192.168.77.5" not in page and "Dysk" not in page      # bez urządzenia nie ma stanu


def test_status_planowana_przerwa(app, user, lab):
    with app.state.engine.begin() as conn:
        maintenance.start(conn, lab["host"], 60, "kernel", "admin")
    page = user.get("/moje/status").text
    assert "Planowana przerwa do" in page and "kernel" not in page


def test_haslo_i_konto_dzialaja_bez_2fa(user):
    page = user.get("/konto")
    assert page.status_code == 200 and "użytkownik" in page.text
    r = user.post("/konto/haslo", data={"csrf_token": csrf_from(page.text), "old": PW, "new": PW + "-nowe", "new2": PW + "-nowe"})
    assert r.status_code == 200 and "Hasło zmienione" in r.text


def test_wylaczenie_wylogowuje(app, user, lab):
    with app.state.engine.begin() as conn:
        shares.set_disabled(conn, lab["uid"], True)
    assert user.get("/moje").status_code == 303


def confirm(app, c, url, data):
    """Pierwszy zapis prosi o hasło; po potwierdzeniu powtarzamy go."""
    r = c.post(url, data=data)
    assert r.status_code == 303 and r.headers["location"].startswith("/potwierdz")
    with app.state.engine.begin() as conn:
        auth.mark_reauth(conn, c.cookies.get("denali_session"))
    return c.post(url, data=data)


def test_administrator_tworzy_konto_i_przydziela(app, admin):
    page = admin.get("/admin/udostepnianie").text
    token = csrf_from(page)
    r = confirm(app, admin, "/admin/udostepnianie/konta", {"csrf_token": token, "login": "bartek"})
    assert r.status_code == 200
    password = re.search(r'data-copy="([^"]+)"', r.text).group(1)
    with app.state.engine.begin() as conn:
        uid = auth.get_user(conn, "bartek")["id"]
        sid = shares.create(conn, "Immich", "link", "https://immich.example.com")
    assert admin.post(f"/admin/udostepnianie/konta/{uid}/uslugi", data={"csrf_token": token, "share": sid}).status_code == 303
    with new_client(app) as c:
        assert do_login(c, login="bartek", password=password).headers["location"] == "/"
        assert "Immich" in c.get("/moje").text


def test_zly_login(app, admin):
    token = csrf_from(admin.get("/admin/udostepnianie").text)
    r = confirm(app, admin, "/admin/udostepnianie/konta", {"csrf_token": token, "login": "zły login"})
    assert r.status_code == 400 and "Login:" in r.text


def test_nie_mozna_ruszyc_konta_administratora(app, admin):
    token = csrf_from(admin.get("/admin/udostepnianie").text)
    with app.state.engine.connect() as conn:
        uid = auth.get_user(conn, "admin")["id"]
    assert confirm(app, admin, f"/admin/udostepnianie/konta/{uid}/haslo", {"csrf_token": token}).status_code == 404
    assert admin.post(f"/admin/udostepnianie/konta/{uid}/wlacz", data={"csrf_token": token, "wylacz": "1"}).status_code == 404
    with app.state.engine.connect() as conn:
        assert not conn.execute(select(app_user.c.disabled).where(app_user.c.id == uid)).scalar()


def test_formularz_uslugi_i_tylko_status(app, admin):
    page = admin.get("/admin/udostepnianie/uslugi/nowa").text
    token = csrf_from(page)
    r = admin.post("/admin/udostepnianie/uslugi/nowa", data={"csrf_token": token, "title": "Zła", "kind": "link", "target": "javascript:alert(1)"})
    assert r.status_code == 400 and 'value="Zła"' in r.text
    r = admin.post("/admin/udostepnianie/uslugi/nowa", data={"csrf_token": token, "title": "Serwer", "kind": "status"})
    assert r.status_code == 400 and "Wybierz urządzenie" in r.text
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "pve", "host")
    r = admin.post("/admin/udostepnianie/uslugi/nowa", data={"csrf_token": token, "title": "Serwer", "kind": "status", "device_id": did})
    assert r.status_code == 303
    assert "Serwer" not in admin.get("/moje").text and "Serwer" in admin.get("/moje/status").text


def test_tailscale_czeka_jako_gotowa_instrukcja(app):
    with app.state.engine.connect() as conn:
        s = conn.execute(select(share).where(share.c.title == "Tailscale")).mappings().first()
    assert s["kind"] == "instrukcja" and "tailscale.com/download" in s["body_md"]


def test_grupa_pocket_id_daje_role_uzytkownika():
    s = Settings(_env_file=None, oidc_user_group="denali-uzytkownicy")
    assert oidc.groups_set(s) and oidc.role_for(s, ["Denali-Uzytkownicy"]) == shares.ROLE
    assert oidc.role_for(s, ["inna"]) is None
