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
    assert [k for k, _l, _u in infra_views.tabs(conn)] == ["mapa", "lista", "adresy", "szafa"]
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
        assert f'href="/urzadzenia/{sw}"' in page and "rack-dev" in page
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
    assert [c["label"] for c in m["columns"]] == ["Sieć", "Serwery i maszyny"]
    [box] = m["boxes"]                                           # serwer = ramka z maszynami w środku
    assert box["id"] == pve and box["count"] == 2 and pve not in {n["id"] for n in m["nodes"]}
    assert phone not in {n["id"] for n in m["nodes"]} and m["hidden_clients"] == 1
    assert sorted(e["cls"] for e in m["lines"]) == ["", "dane broken"]   # kabel do ramki + dane (źródło padło)
    assert phone in {n["id"] for n in mapview.build(conn, {}, {}, with_clients=True)["nodes"]}


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
