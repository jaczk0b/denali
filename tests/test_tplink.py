"""Router TP-Link przez nieoficjalną bibliotekę — na prawdziwych klasach danych biblioteki (bez routera)."""
import asyncio
from ipaddress import IPv4Address

import pytest
from macaddress import EUI48
from sqlalchemy import select
from tplinkrouterc6u import Connection, Device, Firmware, Status

from denali import discovery, integrations, inventory as inv, live, profiles
from denali.adapters import AdapterError
from denali.adapters.tplink import Tplink

CONFIG = {"url": "http://192.168.1.1", "username": "admin", "as_ap": False, "verify_tls": False}
SECRETS = {"password": "haslo-routera"}


def router_status():
    st = Status()
    st._lan_ipv4_addr = IPv4Address("192.168.1.1")
    st._lan_macaddr = EUI48("50-C7-BF-00-00-01")
    st._wan_ipv4_addr = IPv4Address("83.1.2.3")
    st.wifi_clients_total, st.wired_total, st.clients_total, st.cpu_usage = 2, 1, 3, 0.12
    st.devices = [
        Device(Connection.HOST_5G, EUI48("AA-BB-CC-00-00-01"), IPv4Address("192.168.1.50"), "iPhone-Jakub", signal=-55),
        Device(Connection.WIRED, EUI48("BC-24-11-00-00-07"), IPv4Address("192.168.1.20"), "docker"),
        Device(Connection.HOST_2G, EUI48("AA-BB-CC-00-00-02"), IPv4Address("192.168.1.60"), "--"),
    ]
    return st, Firmware("Archer AX23 v1.0", "Archer AX23", "1.1.0 Build 2024")


def reader_ok(url, username, password, verify, timeout=20):
    assert (url, username, password, verify) == ("http://192.168.1.1", "admin", "haslo-routera", False)
    return router_status()


def reader_bad(*a, **kw):
    raise AdapterError("Router odrzucił hasło.")


def run(coro):
    return asyncio.run(coro)


def adapter(reader=reader_ok, **cfg):
    a = Tplink({**CONFIG, **cfg}, SECRETS)
    a.reader = reader
    return a


def test_odczyt_routera_i_klientow():
    res = run(adapter().poll())
    found = {f.external_id: f.proposed for f in res.found}
    assert found["router"] == {"name": "Archer AX23", "kind": "router", "addresses": [
        {"kind": "ip", "value": "192.168.1.1"}, {"kind": "mac", "value": "50:c7:bf:00:00:01"}]}
    assert found["mac/aa:bb:cc:00:00:01"]["kind"] == "telefon" and found["mac/aa:bb:cc:00:00:01"]["name"] == "iPhone-Jakub"
    assert found["mac/bc:24:11:00:00:07"]["kind"] == "komputer"
    assert found["mac/aa:bb:cc:00:00:02"]["name"] == "aa:bb:cc:00:00:02"           # bez nazwy → MAC
    st = {s.external_id: s for s in res.states}
    assert st["router"].summary == "Archer AX23 v1.0 · klienci: 3 (Wi-Fi 2, kabel 1) · CPU 12% · WAN 83.1.2.3"
    assert st["mac/aa:bb:cc:00:00:01"].summary == "w sieci · Wi-Fi 5 GHz · sygnał -55 dBm · 192.168.1.50"


def test_ap_i_bledy():
    assert next(f for f in run(adapter(as_ap=True).poll()).found if f.external_id == "router").proposed["kind"] == "ap"
    assert run(adapter().test()) == "Połączono: Archer AX23 v1.0 · klienci: 3"
    with pytest.raises(AdapterError, match="odrzucił hasło"):
        run(adapter(reader_bad).test())
    with pytest.raises(AdapterError, match="bez ścieżki"):
        Tplink.validate({**CONFIG, "url": "http://192.168.1.1/webpages/login.html"}, SECRETS)


def test_klienci_dopasowuja_sie_a_telefony_nie_robia_awarii(app, monkeypatch):
    from denali.adapters import registry
    monkeypatch.setattr(registry()["tplink"], "reader", staticmethod(reader_ok))
    with app.state.engine.begin() as c:
        vm = inv.create_device(c, "docker", "vm")
        inv.add_address(c, vm, "mac", "bc:24:11:00:00:07")             # znana VM (np. z Proxmoxa / HLC)
        iid = integrations.save(c, app.state.box, "tplink", "router", CONFIG, SECRETS)
        row = integrations.get(c, iid)
    run(integrations.poll_one(app.state.engine, app.state.box, row, now=1000))
    with app.state.engine.begin() as c:
        matches = discovery.suggest_matches(c)
        assert [d["id"] for d in matches.values()] == [vm]               # znany po MAC — bez duplikatu
        discovery.link_matches(c)
        phone_ref = next(r for r in discovery.pending(c) if r["external_id"] == "mac/aa:bb:cc:00:00:01")
        [phone] = discovery.approve(c, phone_ref["id"])
        assert profiles.effective(c, phone)[0] == "Komputery i telefony"
        assert profiles.value(c, phone, "monitor") is False and profiles.value(c, phone, "notify") is False
        live.evaluate(c, now=1000)
        assert phone not in live.saved(c, now=1000)                       # nie monitorowany → nigdy awaria
        [src] = integrations.states_for_device(c, vm)
        assert "kabel" in src["summary"]


def ap_status(clients):
    st = Status()
    st._lan_ipv4_addr = IPv4Address("192.168.1.2")
    st._lan_macaddr = EUI48("50-C7-BF-00-00-02")
    st.wifi_clients_total, st.wired_total, st.clients_total = len(clients), 0, len(clients)
    st.devices = clients
    return st, Firmware("Archer AX10 v1.0", "Archer AX10", "1.3.0")


CAMERA = Device(Connection.HOST_2G, EUI48("AA-BB-CC-00-00-09"), IPv4Address("192.168.1.70"), "kamera")


def test_klient_zalezy_od_ap_przez_ktory_jest_w_sieci(app, monkeypatch):
    from denali.adapters import registry
    from denali.models import dependency
    seen = {"ap": [CAMERA], "router": [], "ap_fails": False}

    def reader(url, username, password, verify, timeout=20):
        if url == "http://192.168.1.2":
            if seen["ap_fails"]:
                raise AdapterError("Brak połączenia z http://192.168.1.2 (ConnectError).")
            return ap_status(seen["ap"])
        st, fw = router_status()
        st.devices = seen["router"] + [Device(Connection.WIRED, EUI48("50-C7-BF-00-00-02"),
                                              IPv4Address("192.168.1.2"), "AP")]
        return st, fw

    monkeypatch.setattr(registry()["tplink"], "reader", staticmethod(reader))
    with app.state.engine.begin() as c:
        cam = inv.create_device(c, "kamera", "inne")
        inv.add_address(c, cam, "mac", "aa:bb:cc:00:00:09")
        rid = integrations.save(c, app.state.box, "tplink", "router", CONFIG, SECRETS)
        aid = integrations.save(c, app.state.box, "tplink", "ap", {**CONFIG, "url": "http://192.168.1.2", "as_ap": True},
                                SECRETS)

    def poll(iid, now):
        with app.state.engine.begin() as c:
            row = integrations.get(c, iid)
        return run(integrations.poll_one(app.state.engine, app.state.box, row, now=now))

    def uplinks(c):
        return {(d.device_id, d.depends_on_id, d.hard, d.source) for d in c.execute(
            select(dependency).where(dependency.c.type == "siec"))}

    poll(rid, 1000), poll(aid, 1000)
    with app.state.engine.begin() as c:
        for r in discovery.pending(c):
            if r["external_id"] == "router":
                discovery.approve(c, r["id"])
        discovery.link_matches(c)                       # kamera po MAC, AP z listy routera po MAC
        ids = {d.name: d.id for d in c.execute(select(inv.device.c.id, inv.device.c.name))}
        router, ap = ids["Archer AX23"], ids["Archer AX10"]
    poll(rid, 1300), poll(aid, 1300)
    with app.state.engine.begin() as c:
        assert uplinks(c) == {(cam, ap, True, f"siec:{aid}"),          # Wi-Fi → twarda od AP
                              (ap, router, False, f"siec:{rid}")}      # AP na kablu → miękka od routera
        [f] = inv.load_graph(c).analyze({ap: "down", cam: "down", router: "ok"})
        assert f.cause == ap and f.effects == [cam]                     # jeden incydent: padł AP, kamera to skutek

    # kamera przeszła do routera
    seen["ap"], seen["router"] = [], [CAMERA]
    poll(rid, 1600), poll(aid, 1600)
    with app.state.engine.begin() as c:
        assert uplinks(c) == {(cam, router, True, f"siec:{rid}"), (ap, router, False, f"siec:{rid}")}

    # wróciła do AP, a potem AP przestał odpowiadać — zależność zostaje, żeby analiza wskazała AP
    seen["ap"], seen["router"] = [CAMERA], []
    poll(rid, 1900), poll(aid, 1900)
    seen["ap_fails"] = True
    assert poll(aid, 2200)
    with app.state.engine.begin() as c:
        assert (cam, ap, True, f"siec:{aid}") in uplinks(c)

    # zależność wpisana ręcznie nie jest ruszana
    seen["ap_fails"], seen["ap"] = False, []
    with app.state.engine.begin() as c:
        c.execute(dependency.delete().where(dependency.c.device_id == cam))
        inv.add_dependency(c, cam, ap, "siec", hard=False, note="ręcznie")
    poll(aid, 2500)
    with app.state.engine.begin() as c:
        assert (cam, ap, False, "manual") in uplinks(c)
