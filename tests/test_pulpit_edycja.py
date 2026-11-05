"""Nowy pulpit: baner z wyciszaniem, Stan labu, kafelki ze stanem, widżety i tryb edycji z cofaniem."""
import asyncio
import re
import time

import pytest

from denali import deskview, desk, incidents, inventory as inv, live, notify, status
from denali.db import kv_set
from denali.models import check_state, incident, integration, observed_state

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


def csrf(client):
    return csrf_from(client.get("/?edycja=1").text)


def api(client, path, csrf_token, /, **data):
    return client.post(f"/pulpit/api/{path}", json=data, headers={"X-CSRF-Token": csrf_token})


def add_tile(app, name, category="Na co dzień", **kw):
    with app.state.engine.begin() as conn:
        return desk.add_tile(conn, desk.ensure_category(conn, category), name, kw.pop("url", f"/{name}"), **kw)


def open_incident(app, title="Pangolin nie działa", **kw):
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "pangolin", "vps")
        iid = incidents.open_manual(conn, title, "jakub", did, kw.pop("severity", "krytyczny"))
    return iid, did


# ───────────────────────── baner ─────────────────────────
def test_baner_pokazuje_co_przyczyne_od_kiedy_i_notatke(app, admin):
    iid, _ = open_incident(app)
    with app.state.engine.begin() as conn:
        conn.execute(incident.update().where(incident.c.id == iid).values(cause_md="Rekord DNS wskazuje adres lokalny\nreszta opisu"))
        incidents.add_note(conn, iid, "Naprawię po przyjściu switcha", "jakub")
    page = admin.get("/").text
    assert 'class="banner-big down"' in page and "Pangolin nie działa" in page
    assert "Rekord DNS wskazuje adres lokalny" in page and "reszta opisu" not in page
    assert "Naprawię po przyjściu switcha" in page and "Wycisz do jutra 8:00" in page


def test_wyciszona_awaria_nie_krzyczy_a_po_czasie_wraca(app, admin):
    iid, _ = open_incident(app)
    r = admin.post(f"/incydenty/{iid}/wycisz", data={"csrf_token": csrf_from(admin.get("/").text), "do": "1h"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = admin.get("/").text
    assert "banner-big" not in page and 'class="banner-line down"' in page and "wyciszone do" in page
    with app.state.engine.begin() as conn:
        conn.execute(incident.update().where(incident.c.id == iid).values(muted_until=time.time() - 1))
    assert "banner-big" in admin.get("/").text


def test_przywrocenie_wyciszenia(app, admin):
    iid, _ = open_incident(app)
    token = csrf_from(admin.get("/").text)
    admin.post(f"/incydenty/{iid}/wycisz", data={"csrf_token": token, "do": "rano"})
    admin.post(f"/incydenty/{iid}/wycisz", data={"csrf_token": token, "do": ""})
    assert "banner-big" in admin.get("/").text


def test_nowa_awaria_rozwija_baner_mimo_wyciszenia(app, admin):
    iid, _ = open_incident(app, "Stara awaria")
    with app.state.engine.begin() as conn:
        incidents.mute(conn, iid, time.time() + 3600, "jakub")
        incidents.open_manual(conn, "Nowa awaria", "jakub", severity="krytyczny")
    page = admin.get("/").text
    assert "banner-big" in page and page.index("Nowa awaria") < page.index("Stara awaria") and "wyciszone do" in page


def test_rozlanie_awarii_zdejmuje_wyciszenie(app):
    from denali import rca
    with app.state.engine.begin() as conn:
        a, b = inv.create_device(conn, "a", "host"), inv.create_device(conn, "b", "vm")
        graph = inv.load_graph(conn)
        iid, _ = incidents.record_finding(conn, graph, rca.Finding(cause=a))
        incidents.mute(conn, iid, time.time() + 3600, "jakub")
        incidents.record_finding(conn, graph, rca.Finding(cause=a))              # bez nowych skutków: dalej cicho
        assert conn.execute(incident.select().where(incident.c.id == iid)).mappings().one()["muted_until"]
        incidents.record_finding(conn, graph, rca.Finding(cause=a, effects=[b]))
        assert conn.execute(incident.select().where(incident.c.id == iid)).mappings().one()["muted_until"] is None


def test_przypomnienia_czekaja_na_koniec_wyciszenia(app, monkeypatch):
    sent = []

    async def deliver(conn, box, payload, severity, transport=None, now=None):
        sent.append(payload)
        return True
    monkeypatch.setattr(notify, "deliver", deliver)
    monkeypatch.setattr(notify, "repeat_hours", lambda conn: 1)
    monkeypatch.setattr(notify, "channels", lambda conn, enabled_only=False: [1])
    now = time.time()
    iid, _ = open_incident(app)
    with app.state.engine.begin() as conn:
        conn.execute(incident.update().where(incident.c.id == iid).values(source="auto", notified_at=now - 7200, muted_until=now + 600))
        assert asyncio.run(notify.reminders(conn, app.state.box, "https://x", now=now)) == 0
        assert asyncio.run(notify.reminders(conn, app.state.box, "https://x", now=now + 601)) == 1
    assert len(sent) == 1


def test_koniec_wyciszenia_zmienia_odcisk_strony(app):
    iid, _ = open_incident(app)
    now = time.time()
    with app.state.engine.begin() as conn:
        conn.execute(incident.update().where(incident.c.id == iid).values(muted_until=now + 100))
        assert live.version(conn, now) != live.version(conn, now + 101)


def test_przycisk_wyciszenia_tylko_dla_admina(app, viewer):
    iid, _ = open_incident(app)
    page = viewer.get("/").text
    assert "Wycisz do" not in page
    assert viewer.post(f"/incydenty/{iid}/wycisz", data={"csrf_token": csrf_from(viewer.get("/procedury").text) if "csrf_token" in viewer.get("/procedury").text else "", "do": "1h"}).status_code == 403


# ───────────────────────── Stan labu i kafelki ─────────────────────────
def test_stan_labu_bez_danych_mowi_to_wprost(app, admin):
    page = admin.get("/").text
    assert "Stan labu" in page and page.count('class="kpi"') == 4 and "Ostatnia kopia NAS" in page
    with app.state.engine.connect() as conn:
        kpis = deskview.kpis(conn, deskview.device_rows(conn), {}, [])
    assert [k["value"] for k in kpis] == ["—", "—", "—", "—"] and all(k["state"] == "unknown" for k in kpis)


def test_stan_labu_liczy_aplikacje_z_domu_i_z_zewnatrz(app):
    with app.state.engine.begin() as conn:
        a = inv.create_device(conn, "a", "usluga", app_url="https://a.example.com")
        b = inv.create_device(conn, "b", "usluga", app_url="https://b.example.com")
        states = {a: {"state": "ok", "why": ""}, b: {"state": "warn", "why": "uwaga"}}
        conn.execute(check_state.insert(), [
            {"device_id": a, "kind": "internet", "target": "https://a.example.com", "ok": False, "checked_at": 1},
            {"device_id": b, "kind": "internet", "target": "https://b.example.com", "ok": True, "checked_at": 1}])
        apps = deskview.kpis(conn, deskview.device_rows(conn), states, deskview.load_checks(conn))[0]
    assert (apps["value"], apps["unit"], apps["state"], apps["note"]) == (2, "/2", "down", "1/2 z zewnątrz")


def test_tylko_w_domu_gdy_z_internetu_nie_dziala(app, admin):
    with app.state.engine.begin() as conn:
        d = inv.create_device(conn, "Immich", "usluga", app_url="https://immich.example.com")
        desk.pin_device(conn, d, desk.ensure_category(conn, "Foto"))
        conn.execute(check_state.insert().values(device_id=d, kind="internet", target="https://immich.example.com", ok=False, checked_at=1))
    assert "tylko w domu" not in admin.get("/").text                    # stan z LAN nieznany: nie zgadujemy
    with app.state.engine.begin() as conn:
        kv_set(conn, live.STATES_KEY, {"at": time.time(), "devices": {str(d): {"state": "ok", "why": ""}}})
    assert "tylko w domu" in admin.get("/").text
    with app.state.engine.begin() as conn:
        desk.save_look(conn, {"mark_lan": False})
    assert "tylko w domu" not in admin.get("/").text


def test_skutek_awarii_zdalnego_dostepu_dziala_w_domu(app):
    with app.state.engine.begin() as conn:
        d = inv.create_device(conn, "Immich", "usluga", app_url="https://immich.example.com")
        cat = desk.ensure_category(conn, "Foto")
        desk.pin_device(conn, d, cat)
        conn.execute(check_state.insert().values(device_id=d, kind="internet", target="https://immich.example.com", ok=False, checked_at=1))
        board = deskview.decorate(desk.board(conn), {d: "skutek"}, {d: {"state": "warn", "why": "12 aktualizacji"}}, deskview.load_checks(conn))
        tile = next(t for c in board for t in c["tiles"])
        assert (tile["lan_only"], tile["status"], tile["status_text"], tile["mark"]) == (True, "warn", "12 aktualizacji", None)
        board = deskview.decorate(desk.board(conn), {d: "skutek"}, {d: {"state": "down", "why": "x"}}, deskview.load_checks(conn))
        tile = next(t for c in board for t in c["tiles"])
        assert (tile["lan_only"], tile["status"]) == (False, "effect")      # w domu też nie działa: bez plakietki


@pytest.mark.parametrize("mark,live_state,expected", [
    ("przyczyna", None, ("down", "Nie działa")), ("skutek", None, ("effect", "Bez danych")), ("gorzej", None, ("warn", "Działa gorzej")),
    (None, {"state": "ok", "why": ""}, ("ok", "Działa")), (None, {"state": "warn", "why": "RAM 91%"}, ("warn", "RAM 91%")),
    (None, {"state": "off", "why": ""}, ("unknown", "Wyłączone")), (None, None, ("unknown", "Brak danych"))])
def test_stan_kafelka(mark, live_state, expected):
    marks = {1: mark} if mark else {}
    assert deskview.tile_status({"device_id": 1}, marks, {1: live_state} if live_state else {}) == expected


def test_skrot_bez_urzadzenia_nie_ma_stanu():
    assert deskview.tile_status({"device_id": None}, {}, {}) is None


# ───────────────────────── widżety ─────────────────────────
def test_widzety_mowia_skad_dane(app, admin):
    page = admin.get("/").text
    assert page.count('class="stat-card') == 6 and "z: TrueNAS" in page and "Brak danych z tego źródła" in page


def test_widzet_z_danych_integracji(app):
    with app.state.engine.begin() as conn:
        iid = conn.execute(integration.insert().values(type="proxmox", name="Proxmox Main", config={})).inserted_primary_key[0]
        conn.execute(observed_state.insert().values(integration_id=iid, external_id="node/pve", state="ok", summary="", observed_at=1,
                                                    data={"cpu": 0.23, "mem": 32 * 2**30, "maxmem": 64 * 2**30, "uptime": 18 * 86400}))
        w = deskview.widgets(conn, desk.widgets(conn), {})[0]
    assert (w["name"], w["source"]) == ("pve", "Proxmox Main")
    assert [b["label"] for b in w["bars"]] == ["CPU", "RAM"] and w["bars"][0]["value"] == "23%" and w["bars"][1]["value"] == "32,0 GB z 64,0 GB"
    assert w["foot"] == "działa 18 dni"


# ───────────────────────── panel boczny ─────────────────────────
def test_kazde_ostrzezenie_ma_powod(app):
    with app.state.engine.begin() as conn:
        d = inv.create_device(conn, "haos", "vm")
        rows, more, good = deskview.attention(conn, [], deskview.device_rows(conn), {d: {"state": "warn", "why": "RAM 91%"}})
    assert [(r["title"], r["detail"]) for r in rows] == [("haos", "RAM 91%")] and more == 0 and good == 0


def test_lancuch_od_podstaw_do_skutkow(app):
    with app.state.engine.begin() as conn:
        pve = inv.create_device(conn, "pve", "host")
        vm = inv.create_device(conn, "docker", "vm", parent_id=pve)
        app_ = inv.create_device(conn, "Immich", "kontener", parent_id=vm)
        iid = incidents.open_manual(conn, "docker nie działa", "x", vm, "krytyczny")
        conn.execute(incidents.incident_device.insert().values(incident_id=iid, device_id=app_, role="skutek"))
        states = {pve: {"state": "ok", "why": "ping 1 ms"}}
        names = {pve: "pve", vm: "docker", app_: "Immich"}
        ban = deskview.banner(conn, status.open_incidents(conn), states, names)
        steps = deskview.chain(conn, ban, states, names)
    assert [(s["name"], s["state"], s["cause"]) for s in steps] == [("pve", "ok", False), ("docker", "down", True), ("Immich", "effect", False)]


# ───────────────────────── tablet ─────────────────────────
def test_potwierdzona_awaria_nie_alarmuje_na_tablecie(app, admin):
    from .test_widoki import pair, pair_code
    iid, _ = open_incident(app)
    pad, _ = pair(app, pair_code(admin))
    assert "alarm" in re.search(r'<body class="([^"]*)"', pad.get("/").text).group(1).split()
    with app.state.engine.begin() as conn:
        incidents.ack(conn, iid, "jakub")
    body = re.search(r'<body class="([^"]*)"', pad.get("/").text).group(1).split()
    assert "alarm" not in body and "attention" in body


# ───────────────────────── tryb edycji ─────────────────────────
def test_tryb_edycji_tylko_dla_admina(app, admin, viewer):
    add_tile(app, "Grafana")
    page = admin.get("/?edycja=1").text
    assert "Edytujesz pulpit" in page and "data-remove" in page and "Sortable.min.js" in page and "data-live" not in page
    plain = admin.get("/").text
    assert "Edytuj pulpit" in plain and "Edytujesz pulpit" not in plain and "Sortable" not in plain
    assert "Edytujesz pulpit" not in viewer.get("/?edycja=1").text


def test_kafelki_w_edycji_nie_sa_linkami(app, admin):
    add_tile(app, "Grafana", url="https://grafana.lan")
    assert 'href="https://grafana.lan"' not in admin.get("/?edycja=1").text


@pytest.mark.parametrize("path,data", [("uklad", {}), ("nazwa", {"id": 1, "name": "x"}), ("usun", {"kind": "ikona", "id": 1}),
                                       ("cofnij", {"token": "x"}), ("widzet", {"id": 1, "size": 2}), ("wyglad", {})])
def test_zapisy_wymagaja_admina_i_tokenu(app, admin, viewer, path, data):
    assert api(admin, path, "zly-token", **data).status_code != 200
    assert api(viewer, path, csrf_from(viewer.get("/procedury").text) if "csrf_token" in viewer.get("/procedury").text else "x", **data).status_code != 200


def test_przeciaganie_miedzy_kategoriami_i_cofniecie(app, admin):
    a, b = add_tile(app, "A"), add_tile(app, "B")
    c = add_tile(app, "C", category="Zarządzanie")
    with app.state.engine.connect() as conn:
        before = desk.snapshot(conn)
        home, ops = [x["id"] for x in desk.categories(conn)][:2]
    token = csrf(admin)
    r = api(admin, "uklad", token, sections=["statystyki", ops, home], tiles={str(home): [b], str(ops): [a, c]}, widgets=before["widgets"][::-1])
    assert r.status_code == 200
    with app.state.engine.connect() as conn:
        board = {x["name"]: [t["name"] for t in x["tiles"]] for x in desk.board(conn)}
        assert board == {"Zarządzanie": ["A", "C"], "Na co dzień": ["B"]}
        assert [s["kind"] for s in desk.sections(desk.board(conn), desk.look(conn))][0] == "statystyki"
        assert [w["id"] for w in desk.widgets(conn)] == before["widgets"][::-1]
    assert api(admin, "uklad", token, **before).status_code == 200
    with app.state.engine.connect() as conn:
        assert desk.snapshot(conn) == before


@pytest.mark.parametrize("bad", [{"sections": [1, 2]}, {"sections": ["statystyki", "x"]}, {"tiles": {"999": [1]}}, {"tiles": {"1": ["x"]}}, {"widgets": "x"}])
def test_zly_uklad_odrzucony(app, admin, bad):
    assert api(admin, "uklad", csrf(admin), **bad).status_code == 400


def test_zmiana_nazwy_kategorii(app, admin):
    add_tile(app, "A")
    with app.state.engine.connect() as conn:
        home, ops = [c["id"] for c in desk.categories(conn)][:2]
    token = csrf(admin)
    assert api(admin, "nazwa", token, id=home, name="Dom").status_code == 200
    r = api(admin, "nazwa", token, id=home, name="zarządzanie")
    assert r.status_code == 400 and "już jest" in r.json()["blad"]
    assert api(admin, "nazwa", token, id=ops, name="  ").status_code == 400


def test_usuniecie_ikony_i_cofniecie(app, admin):
    tile = add_tile(app, "Grafana", url="https://grafana.lan", icon="GF")
    token = csrf(admin)
    r = api(admin, "usun", token, kind="ikona", id=tile)
    assert r.status_code == 200 and r.json()["name"] == "Grafana"
    with app.state.engine.connect() as conn:
        assert desk.tiles(conn) == []
    assert api(admin, "cofnij", token, token=r.json()["token"]).status_code == 200
    with app.state.engine.connect() as conn:
        t = desk.tiles(conn)[0]
    assert (t["name"], t["url"], t["icon"]) == ("Grafana", "https://grafana.lan", "GF")
    assert api(admin, "cofnij", token, token=r.json()["token"]).status_code == 400          # drugi raz się nie da


def test_usuniecie_kategorii_z_kafelkami_i_cofniecie(app, admin):
    add_tile(app, "A", category="Foto")
    with app.state.engine.begin() as conn:
        d = inv.create_device(conn, "Immich", "usluga", app_url="https://immich.lan")
        foto = desk.ensure_category(conn, "Foto")
        desk.pin_device(conn, d, foto)
    token = csrf(admin)
    r = api(admin, "usun", token, kind="kategoria", id=foto).json()
    with app.state.engine.connect() as conn:
        assert "Foto" not in [c["name"] for c in desk.categories(conn)]
    assert api(admin, "cofnij", token, token=r["token"]).status_code == 200
    with app.state.engine.connect() as conn:
        board = {c["name"]: {t["name"] for t in c["tiles"]} for c in desk.board(conn)}
    assert board["Foto"] == {"A", "Immich"}


def test_cofniecie_nie_dubluje_urzadzenia(app, admin):
    with app.state.engine.begin() as conn:
        d = inv.create_device(conn, "Immich", "usluga", app_url="https://immich.lan")
        home = desk.ensure_category(conn, "Foto")
        tile = desk.add_tile(conn, home, device_id=d)
    token = csrf(admin)
    r = api(admin, "usun", token, kind="ikona", id=tile).json()
    with app.state.engine.begin() as conn:
        desk.add_tile(conn, home, device_id=d)
    assert api(admin, "cofnij", token, token=r["token"]).status_code == 200
    with app.state.engine.connect() as conn:
        assert len(desk.tiles(conn)) == 1


def test_usuniecie_widzetu_i_dodanie_z_okna(app, admin):
    with app.state.engine.connect() as conn:
        first = desk.widgets(conn)[0]
    token = csrf(admin)
    assert api(admin, "usun", token, kind="widzet", id=first["id"]).json()["name"] == "Proxmox"
    with app.state.engine.connect() as conn:
        offer = next(i for i in desk.catalog(conn) if i["key"] == "widzet:proxmox")
    assert offer["tab"] == "statystyki"
    r = admin.post("/pulpit/ikony/propozycja", data={"csrf_token": token, "key": "widzet:proxmox"})
    assert r.status_code == 303
    with app.state.engine.connect() as conn:
        assert "proxmox" in [w["kind"] for w in desk.widgets(conn)]


def test_rozmiar_widzetu(app, admin):
    with app.state.engine.connect() as conn:
        first = desk.widgets(conn)[0]["id"]
    token = csrf(admin)
    assert api(admin, "widzet", token, id=first, size=2).status_code == 200
    assert api(admin, "widzet", token, id=first, size=3).status_code == 400
    assert 'class="stat-card wide' in admin.get("/").text


def test_panel_boczny_kolejnosc_i_widocznosc(app, admin):
    token = csrf(admin)
    side = [{"key": "zdarzenia", "on": True}, {"key": "uwaga", "on": False}, {"key": "cudze", "on": True}, {"key": "asystent", "on": True}]
    assert api(admin, "wyglad", token, side=side).status_code == 200
    with app.state.engine.connect() as conn:
        rows = desk.side(conn)
    assert [r["key"] for r in rows][:2] == ["zdarzenia", "uwaga"] and "cudze" not in [r["key"] for r in rows]
    assert next(r for r in rows if r["key"] == "asystent")["on"] is False          # niegotowego nie da się włączyć
    page = admin.get("/").text
    assert "Ostatnie zdarzenia" in page and "Wymaga uwagi" not in page


def test_nowa_kategoria_dostaje_wolna_nazwe(app, admin):
    token = csrf(admin)
    for expected in ("Nowa kategoria", "Nowa kategoria 2"):
        r = admin.post("/pulpit/kategorie", data={"csrf_token": token})
        assert r.status_code == 303 and "#nowa-" in r.headers["location"]
        with app.state.engine.connect() as conn:
            assert expected in [c["name"] for c in desk.categories(conn)]


def test_stara_strona_edycji_prowadzi_na_pulpit(admin):
    r = admin.get("/pulpit/ikony")
    assert r.status_code == 303 and r.headers["location"] == "/?edycja=1"


def test_logo_czeka_w_kafelku(app, admin):
    tile = add_tile(app, "Grafana", url="https://grafana.lan", icon="GF")
    from denali.models import desk_tile
    with app.state.engine.begin() as conn:
        conn.execute(desk_tile.update().where(desk_tile.c.id == tile).values(logo="/static/logo.png"))
    assert "/static/logo.png" not in admin.get("/").text               # domyślnie litera
    with app.state.engine.begin() as conn:
        kv_set(conn, desk.LAYOUT_KEY, {"icons": "logo"})
    assert 'src="/static/logo.png"' in admin.get("/").text
