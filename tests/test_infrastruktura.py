"""Zakładki działu Infrastruktura: Mapa, Proxmox, TrueNAS, Docker, Sieć, Adresy IP, Szafa."""
import asyncio

import pytest

from denali import discovery, infra_views, integrations, inventory as inv, mapview, racks
from denali.adapters import registry

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client
from .test_komodo import CONFIG as KOMODO_CONFIG, SECRETS as KOMODO_SECRETS, komodo_api
from .test_pbs_truenas import PBS_CONFIG, PBS_SECRETS, TN_CONFIG, TN_SECRETS, pbs_api, truenas_rest
from .test_proxmox import CONFIG as PVE_CONFIG, SECRETS as PVE_SECRETS, pve_api
from .test_tplink import CONFIG as TP_CONFIG, SECRETS as TP_SECRETS, reader_ok


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def full(app, monkeypatch):
    """Wszystkie źródła po jednym odczycie, obiekty zatwierdzone."""
    monkeypatch.setattr(registry()["tplink"], "reader", staticmethod(reader_ok))
    sources = [("proxmox", "pve", PVE_CONFIG, PVE_SECRETS, pve_api()), ("pbs", "pbs", PBS_CONFIG, PBS_SECRETS, pbs_api()),
               ("truenas", "truenas", TN_CONFIG, TN_SECRETS, truenas_rest()),
               ("komodo", "komodo", KOMODO_CONFIG, KOMODO_SECRETS, komodo_api()),
               ("tplink", "router", TP_CONFIG, TP_SECRETS, None)]
    with app.state.engine.begin() as c:
        rows = [(integrations.get(c, integrations.save(c, app.state.box, t, n, cfg, sec)), tr)
                for t, n, cfg, sec, tr in sources]
    for row, tr in rows:
        assert run(integrations.poll_one(app.state.engine, app.state.box, row, tr, now=1000)) is None
    with app.state.engine.begin() as c:
        discovery.link_matches(c)
        discovery.approve_all(c)
    return app


def test_zakladki_tylko_dla_podlaczonych_zrodel(app, conn):
    assert [k for k, _l, _u in infra_views.tabs(conn)] == ["mapa", "lista", "adresy"]   # Szafa: gdy jest jakaś
    integrations.save(conn, app.state.box, "komodo", "komodo", KOMODO_CONFIG, KOMODO_SECRETS)
    assert "docker" in [k for k, _l, _u in infra_views.tabs(conn)]


def test_dane_zakladek(full):
    with full.state.engine.connect() as c:
        [pve] = infra_views.proxmox(c)
        assert pve["nodes"] and all(n["device_id"] for n in pve["nodes"])
        assert pve["guests"] and all(g["device_id"] for g in pve["guests"])
        assert any(g["backup"] for g in pve["guests"])                 # kopia z PBS przy maszynie
        [tn] = infra_views.truenas(c)
        assert {p["name"] for p in tn["pools"]} >= {"tank", "kopie"}
        assert next(p for p in tn["pools"] if p["name"] == "tank")["size"]
        [k] = infra_views.docker(c)
        assert k["count"]["stacks"] >= 1 and k["count"]["updates"] == 1
        [net] = infra_views.network(c)
        assert net["count"]["all"] == 3 and net["count"]["wired"] == 1
        clients = infra_views.all_clients([net])
        assert {x["mac"] for x in clients} == {"aa:bb:cc:00:00:01", "bc:24:11:00:00:07", "aa:bb:cc:00:00:02"}


def test_strony_zakladek(full):
    make_user(full)
    with new_client(full) as cl:
        do_login(cl)
        enable_2fa(cl, full)
        for path, text in [("/infrastruktura/mapa", "Mapa infrastruktury"), ("/infrastruktura/proxmox", "Maszyny i kontenery"),
                           ("/infrastruktura/truenas", "tank"),
                           ("/infrastruktura/docker", "Nowsze obrazy"), ("/infrastruktura/siec", "Kto jest w sieci"),
                           ("/infrastruktura/adresy", "192.168.1.0/24"), ("/infrastruktura/szafa", "Dodaj szafę"),
                           ("/urzadzenia", "Od góry to, czego używasz")]:
            r = cl.get(path)
            assert r.status_code == 200, path
            assert text in r.text, path
            assert 'class="tabs infra-tabs"' in r.text, path
        assert cl.get("/infrastruktura").headers["location"] == "/infrastruktura/mapa"
        tn = cl.get("/infrastruktura/truenas").text
        assert "Powiadomienia TrueNAS" in tn and "Nowa wersja TrueNAS" in tn and "informacja" in tn
        assert "odrzucony" not in tn
        assert "Dyski i SMART" in tn and "WD-222" in tn and "Completed: read failure" in tn
        docker = cl.get("/infrastruktura/docker").text
        assert "immich-server:<strong>release</strong>" in docker and "nowszy obraz w rejestrze" in docker
        assert "redis:<strong>6.2-alpine</strong>" in docker


def test_przyciski_akcji_na_zakladkach(full):
    from denali import actions, profiles
    from denali.models import integration
    with full.state.engine.begin() as c:
        [pve] = [r for r in c.execute(integration.select()).mappings() if r["type"] == "proxmox"]
        actions.save_grants(c, pve, ["akcje"], "admin")
        p = profiles.profile_for(c, {"profile_id": None, "kind": "vm"})
        profiles.set_profile_setting(c, p["id"], "actions", ["proxmox.snapshot"])
    make_user(full)
    with new_client(full) as cl:
        do_login(cl)
        enable_2fa(cl, full)
        page = cl.get("/infrastruktura/proxmox").text
        assert "<th>Akcje</th>" in page and "Zrób snapshot…" in page
        assert "/akcje/" in page and "proxmox.snapshot" in page and "proxmox.reboot" not in page
        assert "<th>Akcje</th>" in cl.get("/infrastruktura/docker").text      # Komodo bez tokenu: same kreski
    make_user(full, "domownik", "inne-dlugie-haslo-domownika", role="podglad")
    with new_client(full) as cl:
        do_login(cl, "domownik", "inne-dlugie-haslo-domownika")
        page = cl.get("/infrastruktura/proxmox").text
        assert page.count("Maszyny i kontenery") and "<th>Akcje</th>" not in page and "Zrób snapshot" not in page


def test_zakladka_bez_zrodla_wraca_do_listy(app):
    make_user(app)
    with new_client(app) as cl:
        do_login(cl)
        enable_2fa(cl, app)
        r = cl.get("/infrastruktura/proxmox")
        assert r.status_code == 303 and r.headers["location"] == "/urzadzenia"


def test_adresy_ip_zajete_wolne_konflikt(conn):
    a = inv.create_device(conn, "pve", "host")
    b = inv.create_device(conn, "stary-nas", "nas")
    inv.add_address(conn, a, "ip", "192.168.1.2")
    inv.add_address(conn, b, "ip", "192.168.1.2")              # ten sam adres na dwóch urządzeniach
    inv.add_address(conn, a, "ip", "100.101.102.103")          # Tailscale — nie jest siecią do rozdzielania
    inv.add_address(conn, b, "ip", "192.168.1.10")
    [net] = infra_views.addresses(conn)
    assert net["net"] == "192.168.1.0/24" and net["used"] == 2 and net["free"] == 252
    assert [c["ip"] for c in net["conflicts"]] == ["192.168.1.2"]
    assert net["free_ranges"][:2] == [(1, 1), (3, 9)]


def test_szafa_pozycje_i_kolizje(conn):
    sw = inv.create_device(conn, "switch", "switch")
    ups = inv.create_device(conn, "ups", "ups")
    rid = racks.save_rack(conn, "Szafa 12U", 12)
    racks.place(conn, rid, 1, 2, device_id=ups)
    racks.place(conn, rid, 12, 1, label="patch panel 24")
    with pytest.raises(racks.RackError, match="zajmuje już „ups”"):
        racks.place(conn, rid, 2, 1, device_id=sw)
    with pytest.raises(racks.RackError, match="nie mieści"):
        racks.place(conn, rid, 12, 2, device_id=sw)
    racks.place(conn, rid, 2, 1, device_id=sw, back=True)       # z tyłu na tej samej wysokości — wolno
    racks.place(conn, rid, 11, 1, device_id=sw)                 # przeniesienie: urządzenie stoi w jednym miejscu
    [x] = racks.overview(conn)
    assert [(i["u"], i["back"]) for i in x["entries"] if i["device_id"] == sw] == [(11, False)]
    assert x["front"][0]["item"]["label"] == "patch panel 24" and x["front"][0]["row"] == 1
    assert x["used"] == 4
    assert sum(r["span"] for r in x["front"]) == 12
    with pytest.raises(racks.RackError, match="wyżej"):
        racks.save_rack(conn, "Szafa 12U", 10, rack_id=rid)
    assert racks.for_device(conn, sw)["rack_name"] == "Szafa 12U"


def test_polka_z_komputerem_i_routerem(conn):
    pc = inv.create_device(conn, "komputer", "komputer")
    router = inv.create_device(conn, "router", "router")
    term = inv.create_device(conn, "terminal HP", "komputer")
    rid = racks.save_rack(conn, "Szafa", 12)
    shelf = racks.place(conn, rid, 3, 8, label="dół szafy", shelf=True)
    racks.place(conn, rid, shelf_id=shelf, device_id=pc)
    racks.place(conn, rid, shelf_id=shelf, device_id=router)
    racks.place(conn, rid, shelf_id=shelf, device_id=term, label="na lewo")
    with pytest.raises(racks.RackError, match="postaw na niej"):
        racks.place(conn, rid, 5, 1, label="patch panel")         # w szynach na wysokości półki — nie
    with pytest.raises(racks.RackError, match="na półce"):
        racks.place(conn, rid, shelf_id=shelf, shelf=True, label="półka w półce")
    racks.place(conn, rid, 11, 1, label="patch panel 24")
    [x] = racks.overview(conn)
    [s] = x["shelves"]
    assert [i["name"] for i in s["on_shelf"]] == ["komputer", "router", "terminal HP"]
    assert x["used"] == 9                                          # półka 8 U + patch panel; to na półce nie liczy się
    assert racks.for_device(conn, pc)["u"] == 3
    racks.remove_item(conn, shelf)                                 # wyjęcie półki zdejmuje to, co na niej stoi
    assert racks.for_device(conn, pc) is None


def test_szafa_przez_strone(app):
    make_user(app)
    with app.state.engine.begin() as c:
        sw = inv.create_device(c, "switch", "switch")
    with new_client(app) as cl:
        do_login(cl)
        enable_2fa(cl, app)
        csrf = csrf_from(cl.get("/infrastruktura/szafa").text)
        assert cl.post("/infrastruktura/szafa", data={"csrf_token": csrf, "name": "Szafa", "units": "12"}).status_code == 303
        with app.state.engine.connect() as c:
            [x] = racks.overview(c)
        r = cl.post(f"/infrastruktura/szafa/{x['rack']['id']}/umiesc",
                    data={"csrf_token": csrf, "device_id": str(sw), "u": "20", "height": "1"})
        assert r.status_code == 400 and "się nie mieści" in r.text
        r = cl.post(f"/infrastruktura/szafa/{x['rack']['id']}/umiesc",
                    data={"csrf_token": csrf, "device_id": str(sw), "u": "6", "height": "1"})
        assert r.status_code == 303
        page = cl.get("/infrastruktura/szafa").text
        assert "/infrastruktura/szafa?pozycja=" in page and "rk-dev" in page
        assert "Szafa · U6" in cl.get(f"/urzadzenia/{sw}").text


def test_mapa_kolumny_i_zaleznosci(conn):
    router = inv.create_device(conn, "router", "router")
    pve = inv.create_device(conn, "pve", "host")
    nas = inv.create_device(conn, "truenas", "vm", parent_id=pve)
    immich = inv.create_device(conn, "immich", "vm", parent_id=pve)
    phone = inv.create_device(conn, "telefon", "telefon")
    inv.add_dependency(conn, immich, nas, "dane")
    inv.add_cable(conn, router, pve)
    m = mapview.build(conn, {nas: {"state": "down"}}, {})
    assert [c["label"] for c in m["columns"]] == ["Internet", "Sieć", "Serwery i maszyny"]
    [box] = m["boxes"]                                           # serwer = ramka z maszynami w środku
    assert box["id"] == pve and box["count"] == 2 and pve not in {n["id"] for n in m["nodes"]}
    assert phone not in {n["id"] for n in m["nodes"]} and m["hidden_clients"] == 1
    assert sorted(e["cls"] for e in m["lines"]) == ["", "", "dane broken"]   # internet→router, kabel do ramki, dane
    assert phone in {n["id"] for n in mapview.build(conn, {}, {}, with_clients=True)["nodes"]}


def test_mapa_siec_jako_lancuch(conn):
    """Router za bramą, switch za routerem, AP za switchem — każdy kolumnę dalej, na wysokości poprzedniego."""
    brama = inv.create_device(conn, "FUTRO", "router")
    router = inv.create_device(conn, "Router Main", "router")
    switch = inv.create_device(conn, "switch", "switch")
    ap = inv.create_device(conn, "AP salon", "ap")
    osobny = inv.create_device(conn, "AP garaż", "ap")            # bez zależności — zostaje w pierwszej kolumnie
    inv.add_dependency(conn, router, brama)
    inv.add_dependency(conn, switch, router)
    inv.add_dependency(conn, ap, switch)
    m = mapview.build(conn, {}, {})
    pos = {n["id"]: (n["x"], n["y"]) for n in m["nodes"]}
    assert [c["label"] for c in m["columns"]] == ["Internet", "Sieć", "", "", ""]
    assert pos[brama][0] < pos[router][0] < pos[switch][0] < pos[ap][0]
    assert pos[brama][1] == pos[router][1] == pos[switch][1] == pos[ap][1]
    assert pos[osobny][0] == pos[brama][0] and pos[osobny][1] != pos[brama][1]
    assert all(e["d"].count("C") == 1 and " C " in e["d"] for e in m["lines"])
    phone = inv.create_device(conn, "telefon", "telefon")
    assert {n["id"]: n["x"] for n in mapview.build(conn, {}, {}, with_clients=True)["nodes"]}[phone] == pos[ap][0]


def test_stary_adres_po_dhcp_to_nie_konflikt(full):
    """Telefon dostał z DHCP adres, który w dokumentacji ma jeszcze inne urządzenie — router mówi, kto ma go teraz."""
    with full.state.engine.begin() as c:
        old = inv.create_device(c, "newt", "lxc")
        inv.add_address(c, old, "ip", "192.168.1.50")            # iPhone-Jakub ma teraz .50 (z routera)
        [net] = [n for n in infra_views.addresses(c) if n["net"] == "192.168.1.0/24"]
        cell = next(x for x in net["cells"] if x["ip"] == "192.168.1.50")
        assert cell["state"] == "zajety" and cell["stale"]
        assert cell["owners"][0]["name"] == "iPhone-Jakub" and cell["owners"][1]["stale"]
        assert "192.168.1.50" not in {x["ip"] for x in net["conflicts"]}


def test_szafa_klikanie(app):
    make_user(app)
    with app.state.engine.begin() as c:
        sw = inv.create_device(c, "switch", "switch")
        pc = inv.create_device(c, "komputer", "komputer")
        rid = racks.save_rack(c, "Szafa", 12)
    with new_client(app) as cl:
        do_login(cl)
        enable_2fa(cl, app)
        page = cl.get("/infrastruktura/szafa").text
        csrf = csrf_from(page)
        assert f"/infrastruktura/szafa?szafa={rid}&u=12" in page          # każde wolne U da się kliknąć
        page = cl.get(f"/infrastruktura/szafa?szafa={rid}&u=1").text
        assert "Dodaj w U1" in page and "wolne w górę: 12U" in page
        # półka na dole: kafelek „Półka”, wysokość 4U
        r = cl.post(f"/infrastruktura/szafa/{rid}/umiesc", data={"csrf_token": csrf, "what": "polka", "u": "1", "height": "4"})
        assert r.status_code == 303 and "pozycja=" in r.headers["location"]
        shelf = int(r.headers["location"].split("pozycja=")[1])
        page = cl.get(r.headers["location"]).text
        assert "Postaw na półce" in page and "U1–U4" in page
        r = cl.post(f"/infrastruktura/szafa/{rid}/umiesc", data={"csrf_token": csrf, "what": "urzadzenie",
                                                                  "device_id": str(pc), "shelf_id": str(shelf)})
        assert r.status_code == 303
        # switch w U10, potem 2U i 1U w górę
        r = cl.post(f"/infrastruktura/szafa/{rid}/umiesc", data={"csrf_token": csrf, "what": "urzadzenie",
                                                                  "device_id": str(sw), "u": "10", "height": "1"})
        item = int(r.headers["location"].split("pozycja=")[1])
        assert cl.post(f"/infrastruktura/szafa/pozycja/{item}", data={"csrf_token": csrf, "height": "2"}).status_code == 303
        assert cl.post(f"/infrastruktura/szafa/pozycja/{item}", data={"csrf_token": csrf, "u": "11"}).status_code == 303
        with app.state.engine.connect() as c:
            assert (racks.for_device(c, sw)["u"], racks.for_device(c, sw)["height"]) == (11, 2)
        r = cl.post(f"/infrastruktura/szafa/pozycja/{item}", data={"csrf_token": csrf, "height": "4"})
        assert r.status_code == 400 and "nie mieści" in r.text                  # 11 + 4 > 12
        # półka 1U w górę przenosi komputer
        assert cl.post(f"/infrastruktura/szafa/pozycja/{shelf}", data={"csrf_token": csrf, "u": "2"}).status_code == 303
        with app.state.engine.connect() as c:
            assert racks.for_device(c, pc)["u"] == 2
        r = cl.post(f"/infrastruktura/szafa/{rid}/umiesc", data={"csrf_token": csrf, "what": "urzadzenie", "u": "8"})
        assert r.status_code == 400 and "Wybierz urządzenie" in r.text


def test_szablony_bez_atrybutu_style():
    """CSP (default-src 'self') blokuje style="…" — pasek byłby zawsze pełny. Style z serwera idą w data-style."""
    import re
    from pathlib import Path
    root = Path(infra_views.__file__).parent / "templates"
    bad = [str(p.relative_to(root)) for p in root.rglob("*.html")
           if re.search(r'(?<![\w-])style="', p.read_text(encoding="utf-8"))]
    assert bad == []


def test_mapa_internet_vps_kolory_i_uklad(app, conn):
    """Internet = łącze od dostawcy (stan z uplink), VPS osobno „z zewnątrz”, kolor = podsieć, układ z przeciągania."""
    from denali import uplink
    router = inv.create_device(conn, "router", "router")
    vps = inv.create_device(conn, "vps-ovh", "vps")
    pve = inv.create_device(conn, "pve", "host")
    a = inv.create_device(conn, "docker", "vm", parent_id=pve)
    b = inv.create_device(conn, "hass", "vm", parent_id=pve)
    inv.add_address(conn, a, "ip", "192.168.20.20")
    inv.add_address(conn, b, "ip", "192.168.30.2")
    uplink.record(conn, False, now=1)
    uplink.record(conn, False, now=2)
    m = mapview.build(conn, {}, {})
    assert [c["label"] for c in m["columns"]][:3] == ["Z zewnątrz", "Internet", "Sieć"]
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["internet"]["state"] == "down" and nodes[vps]["x"] < nodes["internet"]["x"]
    assert {(e["a"], e["b"]) for e in m["lines"]} >= {(router, "internet")}
    assert nodes[a]["color"] != nodes[b]["color"] and set(m["nets"]) == {"192.168.20.0/24", "192.168.30.0/24"}
    [box] = m["boxes"]
    mapview.save_layout(conn, {str(pve): [box["x"] + 100, box["y"] + 50], "zly": [1, 2], str(router): [-5, 3]})
    m2 = mapview.build(conn, {}, {})
    n2 = {n["id"]: n for n in m2["nodes"]}
    assert m2["boxes"][0]["x"] == box["x"] + 100 and n2[a]["x"] == nodes[a]["x"] + 100 and n2[a]["y"] == nodes[a]["y"] + 50
    assert n2[router]["x"] == nodes[router]["x"] and m2["arranged"]          # zła pozycja odrzucona
    mapview.save_layout(conn, None)
    assert not mapview.build(conn, {}, {})["arranged"]
