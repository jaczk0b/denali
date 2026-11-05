"""Ikony pulpitu: dowolne skróty, własne kategorie, propozycje, migracja ze starych grup."""
import pytest
from alembic import command
from sqlalchemy import select, text

from denali import desk, integrations, inventory as inv
from denali.db import make_engine
from denali.migrate import alembic_config
from denali.models import desk_category, desk_tile

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client


@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


@pytest.fixture
def viewer(app):
    make_user(app, login="widz", role="podglad")
    with new_client(app) as c:
        do_login(c, login="widz")
        yield c


def category(app, name):
    with app.state.engine.connect() as conn:
        return next(c["id"] for c in desk.categories(conn) if c["name"] == name)


def add(app, category_name="Na co dzień", **kw):
    with app.state.engine.begin() as conn:
        return desk.add_tile(conn, desk.ensure_category(conn, category_name), **kw)


@pytest.mark.parametrize("url", ["https://grafana.lan", "http://192.168.1.5:3000/d/x", "/procedury", "/"])
def test_dobre_adresy(url):
    assert desk.check_url(f" {url} ") == url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "//zly.pl", "/\\zly.pl", "gopher://nas", "https://", "grafana.lan",
                                 "data:text/html,x", "https://a b"])
def test_zle_adresy(url):
    with pytest.raises(desk.DeskError):
        desk.check_url(url)


def test_ikona_z_formularza_trafia_na_pulpit(app, admin):
    home = category(app, "Na co dzień")
    page = admin.get(f"/pulpit/ikony/nowa?kategoria={home}").text
    assert f'<option value="{home}" selected>' in page
    r = admin.post("/pulpit/ikony/nowa", data={"csrf_token": csrf_from(page), "name": "Grafana", "icon": "GF",
                                               "url": "https://grafana.lan", "category_id": home})
    assert r.status_code == 303
    desk_page = admin.get("/").text
    assert "Grafana" in desk_page and 'href="https://grafana.lan" target="_blank"' in desk_page


def test_nowa_kategoria_z_formularza_ikony(app, admin):
    page = admin.get("/pulpit/ikony/nowa").text
    r = admin.post("/pulpit/ikony/nowa", data={"csrf_token": csrf_from(page), "name": "Plex", "url": "https://plex.lan",
                                               "category_id": category(app, "Zarządzanie"), "new_category": "Multimedia"})
    assert r.status_code == 303
    assert '>Multimedia</h2>' in admin.get("/").text


def test_blad_formularza_zachowuje_wpisane(admin):
    page = admin.get("/pulpit/ikony/nowa").text
    r = admin.post("/pulpit/ikony/nowa", data={"csrf_token": csrf_from(page), "name": "Zła", "url": "javascript:alert(1)"})
    assert r.status_code == 400 and "musi zaczynać się" in r.text and 'value="Zła"' in r.text


def test_strona_panelu_otwiera_sie_na_miejscu(app, admin):
    add(app, name="Procedurki", url="/procedury")
    assert 'href="/procedury" >' in admin.get("/").text.replace("\n", " ").replace("  ", " ")


def test_kafel_urzadzenia_dziedziczy_i_pokazuje_przyczyne(app):
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "Immich", "usluga", app_url="https://immich.lan", icon="IM")
        inv.update_device(conn, did, role="zdjęcia")
        tile = desk.add_tile(conn, desk.ensure_category(conn, "Foto"), device_id=did)
        t = desk.get_tile(conn, tile)
        assert (t["name"], t["url"], t["icon"]) == ("Immich", "https://immich.lan", "IM")
        with pytest.raises(desk.DeskError, match="ma już ikonę"):
            desk.add_tile(conn, desk.ensure_category(conn, "Foto"), device_id=did)
        inv.delete_device(conn, did)
        assert desk.get_tile(conn, tile) is None


def test_urzadzenie_bez_adresu_prowadzi_na_karte(app):
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "pve", "host")
        tile = desk.add_tile(conn, desk.ensure_category(conn, "Serwery"), device_id=did)
        assert desk.get_tile(conn, tile)["url"] == f"/urzadzenia/{did}"


def test_formularz_urzadzenia_przypina_i_zdejmuje(app, admin):
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "NAS", "nas")
    ops = category(app, "Zarządzanie")
    page = admin.get(f"/urzadzenia/{did}/edycja").text
    base = {"csrf_token": csrf_from(page), "name": "NAS", "kind": "nas"}
    assert admin.post(f"/urzadzenia/{did}/edycja", data={**base, "desk_category": ops}).status_code == 303
    assert f'<option value="{ops}" selected>' in admin.get(f"/urzadzenia/{did}/edycja").text
    assert admin.post(f"/urzadzenia/{did}/edycja", data=base).status_code == 303      # bez pola: bez zmian
    with app.state.engine.connect() as conn:
        assert desk.device_category(conn, did)["id"] == ops
    assert admin.post(f"/urzadzenia/{did}/edycja", data={**base, "desk_category": ""}).status_code == 303
    with app.state.engine.connect() as conn:
        assert desk.device_category(conn, did) is None


def test_propozycje_z_urzadzen_integracji_i_stron(app, admin):
    with app.state.engine.begin() as conn:
        inv.create_device(conn, "Grafana", "usluga", app_url="https://grafana.lan")
        integrations.save(conn, app.state.box, "pbs", "Kopie", {"url": "https://pbs.lan:8007", "token_id": "a@pbs!b"},
                          {"token_secret": "c"})
    page = admin.get("/?edycja=1").text
    with app.state.engine.connect() as conn:
        names = {i["name"] for i in desk.catalog(conn) if i["key"]}
    assert {"Grafana", "Kopie", "Mapa", "Integracje"} <= names and not {"Pulpit", "Konto i sesje"} & names
    assert "<b>Grafana</b>" in page
    ops = category(app, "Zarządzanie")
    with app.state.engine.connect() as conn:
        key = next(s["key"] for s in desk.suggestions(conn) if s["name"] == "Kopie")
    r = admin.post("/pulpit/ikony/propozycja", data={"csrf_token": csrf_from(page), "key": key, "category_id": ops})
    assert r.status_code == 303 and r.headers["location"] == f"/?edycja=1&dodaj={ops}"
    page = admin.get(r.headers["location"]).text
    assert f'data-open-add="{ops}"' in page and "Na pulpicie" in page
    # Podwójne kliknięcie nie dubluje ikony.
    r = admin.post("/pulpit/ikony/propozycja", data={"csrf_token": csrf_from(page), "key": key, "category_id": ops})
    assert r.status_code == 400
    with app.state.engine.connect() as conn:
        assert [t["name"] for t in desk.tiles(conn)] == ["Kopie"]


def test_propozycja_nie_przyjmuje_cudzego_adresu(admin):
    page = admin.get("/?edycja=1").text
    r = admin.post("/pulpit/ikony/propozycja", data={"csrf_token": csrf_from(page), "key": "strona:javascript:alert(1)"})
    assert r.status_code == 400


def test_bez_kategorii_powstaje_domyslna(app, admin):
    with app.state.engine.begin() as conn:
        conn.execute(desk_category.delete())
    page = admin.get("/pulpit/ikony/nowa").text
    r = admin.post("/pulpit/ikony/nowa", data={"csrf_token": csrf_from(page), "name": "A", "url": "/a"})
    assert r.status_code == 303
    assert "Na co dzień" in admin.get("/").text


def test_podglad_widzi_ikony_ale_nie_zmienia(app, viewer):
    add(app, name="Grafana", url="https://grafana.lan")
    add(app, name="Audyt", url="/admin/audyt")
    page = viewer.get("/").text
    assert "Grafana" in page and "Audyt" not in page
    assert "Zarządzanie" not in page and "edycja=1" not in page            # pusta kategoria i edycja są ukryte
    assert viewer.get("/pulpit/ikony").status_code == 403
    assert viewer.post("/pulpit/kategorie", data={"csrf_token": csrf_from(page) if "csrf_token" in page else "", "name": "X"}).status_code == 403


def test_ikony_w_wyszukiwarce(app, admin):
    add(app, name="Grafana", url="https://grafana.lan")
    assert {"label": "Grafana", "sub": "ikona na pulpicie", "url": "https://grafana.lan"} in admin.get("/szukaj/szybko?q=graf").json()
    assert "Ikony pulpitu" in admin.get("/szukaj?q=graf").text


def test_migracja_przenosi_przypiete_urzadzenia(settings):
    cfg = alembic_config(settings)
    command.downgrade(cfg, "0018")
    engine = make_engine(settings.db_url)
    insert = text("INSERT INTO device (name, kind, role, criticality, place, serial, notes_md, icon, app_url, pulpit_group, "
                  "created_at, updated_at) VALUES (:n, 'usluga', '', 3, '', '', '', '', '', :g, 0, 0)")
    with engine.begin() as conn:
        ids = {n: conn.execute(insert, {"n": n, "g": g}).lastrowid
               for n, g in [("b", "codzien"), ("a", "codzien"), ("c", "zarzadzanie"), ("d", "brak")]}
        conn.execute(text("INSERT INTO device_address (device_id, kind, value, source, is_primary) "
                          "VALUES (:d, 'ip', '10.0.0.1', 'manual', 0)"), {"d": ids["a"]})
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        board = {c["name"]: [t["name"] for t in c["tiles"]] for c in desk.board(conn)}
        assert board == {"Na co dzień": ["a", "b"], "Zarządzanie": ["c"]}
        assert conn.execute(text("SELECT count(*) FROM device_address")).scalar() == 1      # dzieci device nietknięte
    command.downgrade(cfg, "0018")
    with engine.connect() as conn:
        back = dict(conn.execute(text("SELECT name, pulpit_group FROM device")).all())
    command.upgrade(cfg, "head")
    engine.dispose()
    assert back == {"a": "codzien", "b": "codzien", "c": "zarzadzanie", "d": "brak"}


@pytest.mark.parametrize("url", ["\\\\nas\\zdjecia", "\\\\nas\\Moje Dokumenty\\", "smb://nas/zdjecia", "NFS://nas/tank", "ssh://root@10.0.0.1"])
def test_adres_do_skopiowania(url):
    assert desk.check_url(url) == url and desk.is_copy(url)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "\\\\", "smb://", "file:///etc/passwd", "//host/x"])
def test_niebezpieczne_adresy_odrzucone(url):
    with pytest.raises(desk.DeskError):
        desk.check_url(url)


def test_ikona_kopiuje_zamiast_linku(app, admin):
    add(app, name="Zdjęcia", url="\\\\nas\\zdjecia")
    page = admin.get("/").text
    assert 'data-copy="\\\\nas\\zdjecia"' in page and 'href="\\\\nas' not in page
    with app.state.engine.connect() as conn:
        assert desk.find(conn, "zdj") == []                     # szybkie przejście nie ma dokąd przejść


def test_terminal_i_udzial_w_propozycjach(app, admin):
    from denali import shares
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "docker", "vm")
        inv.add_address(conn, did, "ip", "10.0.0.5")
        inv.create_device(conn, "bez-adresu", "vm")
        shares.create(conn, "Dysk", "adres", "\\\\nas\\wspolne")
        got = {s["key"]: s for s in desk.suggestions(conn)}
        assert got[f"strona:/terminale/{did}"]["name"] == "Terminal: docker"
        assert not any("bez-adresu" in s["name"] for s in got.values())
        tid = desk.add_suggestion(conn, "adres:\\\\nas\\wspolne", category(app, "Na co dzień"))
        assert desk.get_tile(conn, tid)["copy_only"]
    assert f'value="/terminale/{did}"' in admin.get("/pulpit/ikony/nowa").text


def test_podglad_nie_widzi_skrotu_do_terminala(app, admin, viewer):
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "docker", "vm")
    add(app, name="Term", url=f"/terminale/{did}")
    assert f'href="/terminale/{did}"' in admin.get("/").text
    assert f'href="/terminale/{did}"' not in viewer.get("/").text
