"""Router OpenWrt przez ubus (na nagranych odpowiedziach rpcd, bez routera): odczyt, klienci, aktualizacje, ACL,
kopia konfiguracji zaszyfrowana przy urządzeniu i jej pobranie po potwierdzeniu hasłem."""
import asyncio
import gzip
import json

import httpx
import pytest
from sqlalchemy import select

from denali import actions, auth, configs, discovery, integrations, profiles
from denali.adapters import AdapterError
from denali.adapters.openwrt import Openwrt, band, leases, neighbours, networks, speed, upgradable
from denali.models import audit_log, config_backup, update_item

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

CONFIG = {"url": "https://192.168.1.1", "username": "denali", "as_ap": False, "verify_tls": False}
SECRETS = {"password": "haslo-odczytu"}
BACKUP = gzip.compress(b"etc/config/wireless: option key 'tajne-haslo-wifi'")

BOARD = {"hostname": "brama", "model": "TP-Link Archer AX23 v1", "board_name": "tplink,archer-ax23-v1",
         "release": {"distribution": "OpenWrt", "version": "24.10.0", "target": "ramips/mt7621",
                     "description": "OpenWrt 24.10.0 r28427-6df0e3d02a"}}
IFACES = {"interface": [
    {"interface": "lan", "up": True, "l3_device": "br-lan", "proto": "static",
     "ipv4-address": [{"address": "192.168.1.1", "mask": 24}]},
    {"interface": "wan", "up": True, "uptime": 90000, "l3_device": "wan", "proto": "dhcp",
     "ipv4-address": [{"address": "83.1.2.3", "mask": 24}]},
]}
DEVICES = {
    "br-lan": {"type": "bridge", "up": True, "macaddr": "50:c7:bf:00:00:01",
               "bridge-members": ["lan1", "lan2", "lan3", "phy0-ap0"]},
    "lan1": {"type": "Network device", "up": True, "carrier": True, "speed": "1000F", "macaddr": "50:c7:bf:00:00:01"},
    "lan2": {"type": "Network device", "up": True, "carrier": False, "speed": "-1", "macaddr": "50:c7:bf:00:00:01"},
    "lan3": {"type": "Network device", "up": True, "carrier": True, "speed": "100H", "macaddr": "50:c7:bf:00:00:01"},
    "wan": {"type": "Network device", "up": True, "carrier": True, "speed": "1000F", "macaddr": "50:c7:bf:00:00:02"},
}
LEASES = ("1800000000 aa:bb:cc:00:00:01 192.168.1.50 iPhone-Jakub 01:aa:bb:cc:00:00:01\n"
          "1800000000 bc:24:11:00:00:07 192.168.1.20 docker *\n"
          "1800000000 aa:bb:cc:00:00:05 192.168.1.80 stary-laptop *\n")      # dzierżawa jest, urządzenia nie ma
NEIGH = ("192.168.1.20 dev br-lan lladdr bc:24:11:00:00:07 REACHABLE\n"
         "192.168.1.50 dev br-lan lladdr aa:bb:cc:00:00:01 STALE\n"
         "192.168.1.99 dev br-lan  FAILED\n"
         "83.1.2.1 dev wan lladdr 00:11:22:33:44:55 REACHABLE\n")             # brama dostawcy — nie klient
OPKG_OUT = "luci-base - 24.10.0-r1 - 24.10.1-r1\nkmod-6lowpan - 6.6.73-1 - 6.6.74-1\n"


def ubus(deny=(), opkg=OPKG_OUT, backup_status=200, seen=None, wrong_password=False):
    """Router: /ubus (JSON-RPC rpcd) i /cgi-bin/cgi-backup. deny: {„obiekt metoda”} bez prawa w ACL."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if request.url.path == "/cgi-bin/cgi-backup":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form.get("sessionid") != "sid-kopia" or backup_status == 403:
                return httpx.Response(403, text="Permission denied")
            return httpx.Response(200, content=BACKUP, headers={"Content-Type": "application/x-targz"})
        body = json.loads(request.content)
        sid, obj, method, args = body["params"]
        reply = lambda result: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        if obj == "session" and method == "login":
            ok = {"denali": "haslo-odczytu", "denali-kopia": "haslo-kopii"}
            if wrong_password or ok.get(args["username"]) != args["password"]:
                return reply([6])
            return reply([0, {"ubus_rpc_session": "sid-kopia" if args["username"] == "denali-kopia" else "sid-odczyt"}])
        assert sid == "sid-odczyt"
        what = f"{obj} {method}"
        if what in deny or (obj == "file" and f"file {args.get('path') or args.get('command')}" in deny):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "error": {"code": -32002, "message": "Access denied"}})
        if what == "system board":
            return reply([0, BOARD])
        if what == "system info":
            return reply([0, {"uptime": 360000, "load": [32768, 0, 0],
                              "memory": {"total": 128_000_000, "available": 64_000_000}}])
        if what == "network.interface dump":
            return reply([0, IFACES])
        if what == "network.device status":
            return reply([0, DEVICES])
        if what == "iwinfo devices":
            return reply([0, {"devices": ["phy0-ap0", "phy1-ap0", "phy1-sta0"]}])
        if what == "iwinfo info":
            return reply([0, {"phy0-ap0": {"ssid": "Dom", "frequency": 2437, "channel": 6, "mode": "Master"},
                              "phy1-ap0": {"ssid": "Dom", "frequency": 5180, "channel": 36, "mode": "Master"},
                              "phy1-sta0": {"ssid": "Sasiad", "frequency": 5180, "mode": "Client"}}[args["device"]]])
        if what == "iwinfo assoclist":
            return reply([0, {"results": {"phy0-ap0": [{"mac": "AA:BB:CC:00:00:02", "signal": -80}],
                                          "phy1-ap0": [{"mac": "AA:BB:CC:00:00:01", "signal": -55}]}.get(args["device"], [])}])
        if what == "file read" and args["path"] == "/tmp/dhcp.leases":
            return reply([0, {"data": LEASES}])
        if what == "file exec":
            cmd = " ".join([args["command"], *args.get("params", [])])
            if cmd == "/sbin/ip -4 neigh show":
                return reply([0, {"code": 0, "stdout": NEIGH}])
            if cmd == "/bin/opkg list-upgradable" and opkg is not None:
                return reply([0, {"code": 0, "stdout": opkg}])
            return reply([4])          # nie ma takiego programu (np. opkg na OpenWrt z apk)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Object not found"}})
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def adapter(transport, action_secrets=None, **cfg):
    return Openwrt({**CONFIG, **cfg}, SECRETS, transport=transport, action_secrets=action_secrets)


# ───────────────────────── parsowanie ─────────────────────────
def test_parsowanie_wyjscia_routera():
    assert leases(LEASES)["bc:24:11:00:00:07"] == ("192.168.1.20", "docker")
    assert leases("1 aa:bb:cc:00:00:09 192.168.1.9 * *")["aa:bb:cc:00:00:09"] == ("192.168.1.9", "")
    assert neighbours(NEIGH, {"wan"}) == {"bc:24:11:00:00:07": "192.168.1.20", "aa:bb:cc:00:00:01": "192.168.1.50"}
    assert (band(2412), band(5500), band(5955), band(None)) == ("HOST_2G", "HOST_5G", "HOST_6G", "HOST_2G")
    assert (speed("1000F"), speed("2500F"), speed("100H"), speed("-1")) == ("1 Gb/s", "2.5 Gb/s", "100 Mb/s, półdupleks", "")
    assert upgradable(OPKG_OUT) == [("luci-base", "24.10.0-r1", "24.10.1-r1"), ("kmod-6lowpan", "6.6.73-1", "6.6.74-1")]
    apk = ("kmod-6lowpan-6.6.74-r1 aarch64_cortex-a53 {linux} (GPL-2.0) [upgradable from: kmod-6lowpan-6.6.73-r1]\n"
           "libustream-mbedtls20201210-2024.07.28~99bd3d2b-r2 x86_64 {ustream-ssl} (ISC) "
           "[upgradable from: libustream-mbedtls20201210-2024.07.28~99bd3d2b-r1]\n")
    assert upgradable(apk, apk=True) == [("kmod-6lowpan", "6.6.73-r1", "6.6.74-r1"),
                                         ("libustream-mbedtls20201210", "2024.07.28~99bd3d2b-r1", "2024.07.28~99bd3d2b-r2")]


# ───────────────────────── odczyt ─────────────────────────
def test_odczyt_routera_klientow_i_aktualizacji():
    res = run(adapter(ubus()).poll())
    found = {f.external_id: f.proposed for f in res.found}
    assert found["router"] == {"name": "brama", "kind": "router", "addresses": [
        {"kind": "ip", "value": "192.168.1.1"}, {"kind": "mac", "value": "50:c7:bf:00:00:01"}]}
    # Wi-Fi: na pewno w sieci (także bez dzierżawy); kabel: z tablicy sąsiadów; sama dzierżawa to nie obecność
    assert set(found) == {"router", "mac/aa:bb:cc:00:00:01", "mac/aa:bb:cc:00:00:02", "mac/bc:24:11:00:00:07"}
    phone = found["mac/aa:bb:cc:00:00:01"]
    assert phone["kind"] == "telefon" and phone["name"] == "iPhone-Jakub" and phone["uplink"] == {"ref": "router", "hard": True}
    assert found["mac/bc:24:11:00:00:07"]["uplink"] == {"ref": "router", "hard": False}
    assert found["mac/aa:bb:cc:00:00:02"]["name"] == "aa:bb:cc:00:00:02"
    st = {s.external_id: s for s in res.states}
    assert st["mac/aa:bb:cc:00:00:01"].summary == "w sieci · Wi-Fi 5 GHz „Dom” · sygnał -55 dBm · 192.168.1.50"
    assert st["mac/aa:bb:cc:00:00:01"].data["connection"] == "HOST_5G"
    assert st["mac/bc:24:11:00:00:07"].summary == "w sieci · kabel · 192.168.1.20"
    router = st["router"]
    assert router.state == "ok"
    assert router.summary == ("TP-Link Archer AX23 v1 · OpenWrt 24.10.0 r28427-6df0e3d02a · klienci: 3 (Wi-Fi 2, kabel 1)"
                              " · WAN 83.1.2.3 · aktualizacje: 2")
    assert router.data["ports"] == [{"name": "lan1", "up": True, "speed": "1 Gb/s"}, {"name": "lan2", "up": False, "speed": ""},
                                    {"name": "lan3", "up": True, "speed": "100 Mb/s, półdupleks"},
                                    {"name": "wan", "up": True, "speed": "1 Gb/s"}]
    assert [w["ssid"] for w in router.data["wifi"]] == ["Dom", "Dom"]      # bez połączenia klienta (sta)
    assert router.data["memory_used"] == 50 and router.data["load"] == 0.5
    assert router.data["networks"] == [{"name": "lan", "cidr": "192.168.1.0/24", "gateway": "192.168.1.1",
                                        "device": "br-lan", "vlan": None}]          # bez WAN-u
    assert [(u.component, u.current, u.available) for u in res.updates][0] == ("luci-base", "24.10.0-r1", "24.10.1-r1")


def test_wan_nie_dziala_i_ap():
    down = {"interface": [IFACES["interface"][0], {**IFACES["interface"][1], "up": False, "ipv4-address": []}]}

    def transport(request):
        body = json.loads(request.content) if request.url.path == "/ubus" else {}
        if body.get("params", [None, None, None])[1:3] == ["network.interface", "dump"]:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [0, down]})
        return ubus().handler(request)
    st = next(s for s in run(adapter(httpx.MockTransport(transport)).poll()).states if s.external_id == "router")
    assert st.state == "warn" and "WAN nie działa" in st.summary
    res = run(adapter(httpx.MockTransport(transport), as_ap=True).poll())
    assert res.found[0].proposed["kind"] == "ap"
    assert next(s for s in res.states if s.external_id == "router").state == "ok"      # AP nie ma WAN-u


def test_braki_uprawnien_nie_zatrzymuja_odczytu():
    res = run(adapter(ubus(deny={"file /sbin/ip", "file /bin/opkg", "file /usr/bin/apk"})).poll())
    assert res.updates is None                                         # lista aktualizacji zostaje bez zmian
    found = {f.external_id for f in res.found}
    assert "mac/aa:bb:cc:00:00:05" in found                            # bez tablicy sąsiadów — z dzierżaw DHCP
    msg = run(adapter(ubus(deny={"file /bin/opkg", "file /usr/bin/apk"})).test())
    assert msg == "Połączono: TP-Link Archer AX23 v1 · OpenWrt 24.10.0 r28427-6df0e3d02a · bez uprawnień: aktualizacje (ACL ze skryptu)"
    assert run(adapter(ubus(opkg="")).poll()).updates == []            # sprawdzone, nic nie ma


def test_bledy():
    with pytest.raises(AdapterError, match="odrzucił login „denali” albo hasło"):
        run(adapter(ubus(wrong_password=True)).test())
    with pytest.raises(AdapterError, match="system board"):
        run(adapter(ubus(deny={"system board"})).test())
    with pytest.raises(AdapterError, match="nie ma /ubus"):
        run(adapter(httpx.MockTransport(lambda r: httpx.Response(404))).test())
    with pytest.raises(AdapterError, match="bez ścieżki"):
        Openwrt.validate({**CONFIG, "url": "https://192.168.1.1/cgi-bin/luci"}, SECRETS)


def test_skrypt_nadaje_dokladnie_to_co_czyta_adapter():
    acl = json.loads(Openwrt.setup_script.split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0])
    read = acl["denali"]["read"]
    assert "write" not in acl["denali"] and "write" not in acl["denali-kopia"]
    assert acl["denali-kopia"]["read"] == {"cgi-io": ["backup"]}       # konto do kopii nie czyta nic innego
    assert "cgi-io" not in read                                         # konto do odczytu nie zrobi kopii
    seen = []
    run(adapter(ubus(seen=seen)).poll())
    for r in seen:
        _, obj, method, args = json.loads(r.content)["params"]
        if obj == "session":
            continue
        assert method in read["ubus"][obj]
        if obj == "file":
            target = args.get("path") or " ".join([args["command"], *args["params"]])
            assert target in read["file"]


# ───────────────────────── kopia konfiguracji ─────────────────────────
def test_akcja_kopii_osobnym_kontem():
    res = run(adapter(ubus(), {"backup_username": "denali-kopia", "backup_password": "haslo-kopii"})
              .run_action("backup", "router", {"hostname": "brama"}, {}))
    assert res.ok and res.file == {"name": "backup-brama.tar.gz", "data": BACKUP}
    with pytest.raises(AdapterError, match="nie ma prawa do kopii"):
        run(adapter(ubus(backup_status=403), {"backup_username": "denali-kopia", "backup_password": "haslo-kopii"})
            .run_action("backup", "router", {}, {}))
    with pytest.raises(AdapterError, match="odrzucił login „denali-kopia”"):
        run(adapter(ubus(), {"backup_username": "denali-kopia", "backup_password": "zle"})
            .run_action("backup", "router", {}, {}))


@pytest.fixture
def router(app):
    """OpenWrt z kontem do kopii, router zatwierdzony, kopia dozwolona w profilu."""
    app.state.adapter_transport = ubus()
    with app.state.engine.begin() as c:
        iid = integrations.save(c, app.state.box, "openwrt", "brama", dict(CONFIG), dict(SECRETS))
        row = integrations.get(c, iid)
    assert run(integrations.poll_one(app.state.engine, app.state.box, row, ubus())) is None
    with app.state.engine.begin() as c:
        ref = next(r for r in discovery.pending(c) if r["external_id"] == "router")
        [dev] = discovery.approve(c, ref["id"])
        actions.save_credentials(c, app.state.box, integrations.get(c, iid),
                                 {"backup_username": "denali-kopia", "backup_password": "haslo-kopii"}, "admin")
        p = profiles.profile_for(c, {"profile_id": None, "kind": "router"})
        profiles.set_profile_setting(c, p["id"], "actions", ["openwrt.backup"])
        updates = c.execute(select(update_item.c.component).where(update_item.c.integration_id == iid)).scalars().all()
    assert sorted(updates) == ["kmod-6lowpan", "luci-base"]
    return {"iid": iid, "device": dev, "ref": ref["id"]}


def test_kopia_zaszyfrowana_bez_duplikatow(app, router):
    for _ in range(2):
        with app.state.engine.begin() as c:
            run_id = actions.request(c, device_id=router["device"], ref_id=router["ref"], key="openwrt.backup",
                                     user="admin", role="admin")
        run(actions.execute(app.state.engine, app.state.box, run_id, ubus()))
    with app.state.engine.connect() as c:
        assert "bez zmian od poprzedniej kopii" in actions.get(c, run_id)["result"]
        rows = c.execute(select(config_backup)).mappings().all()
        assert len(rows) == 1 and b"tajne" not in rows[0]["data_ct"].encode()
        assert rows[0]["source"] == "brama" and rows[0]["taken_by"] == "admin"
    with app.state.engine.begin() as c:
        with pytest.raises(PermissionError):
            configs.download(c, app.state.box, router["device"], rows[0]["id"], role="admin", reauth_fresh=False, user="x")
        name, data = configs.download(c, app.state.box, router["device"], rows[0]["id"], role="admin",
                                      reauth_fresh=True, user="admin")
    assert data == BACKUP and name.startswith("backup-brama-") and name.endswith(".tar.gz")


def test_starsze_kopie_znikaja(app, conn):
    box = app.state.box
    from denali import inventory as inv
    dev = inv.create_device(conn, "brama", "router")
    for i in range(configs.KEEP + 3):
        configs.store(conn, box, dev, "k.tar.gz", f"wersja {i}".encode(), now=1000 + i)
    kept = configs.for_device(conn, dev)
    assert len(kept) == configs.KEEP and kept[0]["taken_at"] == 1000 + configs.KEEP + 2


def test_panel_kopia_i_pobranie_po_potwierdzeniu(app, router):
    make_user(app, "admin")
    cli = new_client(app)
    do_login(cli, "admin")
    enable_2fa(cli, app, "admin")
    page = cli.get(f"/urzadzenia/{router['device']}/akcje/{router['ref']}/openwrt.backup")
    assert page.status_code == 200
    r = cli.post(f"/urzadzenia/{router['device']}/akcje/{router['ref']}/openwrt.backup",
                 data={"csrf_token": csrf_from(page.text)})
    assert r.status_code == 303                       # kopia nie jest niebezpieczna — bez pytania o hasło
    card = cli.get(f"/urzadzenia/{router['device']}").text
    assert "Kopie konfiguracji" in card
    with app.state.engine.connect() as c:
        bid = c.execute(select(config_backup.c.id)).scalar_one()
    url = f"/urzadzenia/{router['device']}/kopie/{bid}/pobierz"
    r = cli.post(url, data={"csrf_token": csrf_from(card)})
    assert r.status_code == 303 and r.headers["location"].startswith("/potwierdz")
    with app.state.engine.begin() as c:
        auth.mark_reauth(c, cli.cookies.get("denali_session"))
    r = cli.post(url, data={"csrf_token": csrf_from(card)})
    assert r.status_code == 200 and r.content == BACKUP and "attachment" in r.headers["content-disposition"]
    with app.state.engine.connect() as c:
        assert c.execute(select(audit_log).where(audit_log.c.action == "config_backup_download")).first()


def test_zakladka_siec_i_strona_integracji(app, router):
    make_user(app, "admin")
    cli = new_client(app)
    do_login(cli, "admin")
    enable_2fa(cli, app, "admin")
    html = cli.get("/infrastruktura/siec", follow_redirects=True).text
    assert "OpenWrt 24.10.0" in html and "Pakiety do aktualizacji na routerze: 2" in html and "83.1.2.3" in html
    assert "lan3" in html and "100 Mb/s, półdupleks" in html and "„Dom”" in html and "iPhone-Jakub" in html
    assert "Sieci (VLAN-y)" in html and "192.168.1.0/24" in html
    form = cli.get("/admin/integracje/nowa?typ=openwrt").text
    assert "uhttpd -m" in form and "denali-kopia" in form
    page = cli.get(f"/admin/integracje/{router['iid']}").text
    assert "Akcje — osobny token" in page and "denali-kopia" in page


def test_sieci_i_vlany_z_routera():
    ifaces = [
        {"interface": "lan", "l3_device": "br-lan", "ipv4-address": [{"address": "192.168.10.1", "mask": 24}]},
        {"interface": "iot", "l3_device": "br-lan.30", "ipv4-address": [{"address": "192.168.30.1", "mask": 23}]},
        {"interface": "wan", "l3_device": "wan", "ipv4-address": [{"address": "83.1.2.3", "mask": 24}]},
        {"interface": "loopback", "l3_device": "lo", "ipv4-address": [{"address": "127.0.0.1", "mask": 8}]},
        {"interface": "zly", "ipv4-address": [{"address": "x", "mask": "y"}]},
    ]
    assert networks(ifaces) == [
        {"name": "lan", "cidr": "192.168.10.0/24", "gateway": "192.168.10.1", "device": "br-lan", "vlan": None},
        {"name": "iot", "cidr": "192.168.30.0/23", "gateway": "192.168.30.1", "device": "br-lan.30", "vlan": 30}]


def test_adresy_i_mapa_z_vlanow_routera(app, router):
    from denali import infra_views, inventory as inv, mapview
    from denali.models import observed_state
    with app.state.engine.begin() as c:
        c.execute(observed_state.update().where(observed_state.c.external_id == "router").values(data={"networks": [
            {"name": "lan", "cidr": "192.168.1.0/24", "vlan": None},
            {"name": "iot", "cidr": "192.168.30.0/23", "vlan": 30}]}))
        cam = inv.create_device(c, "kamera", "inne")
        inv.add_address(c, cam, "ip", "192.168.31.7")                  # druga połowa /23 — ta sama sieć
        nets = {n["net"]: n for n in infra_views.addresses(c)}
        assert nets["192.168.30.0/23"]["name"] == "iot · VLAN 30" and nets["192.168.30.0/23"]["routed"]
        assert len(nets["192.168.30.0/23"]["cells"]) == 510 and "192.168.31.0/24" not in nets
        m = mapview.build(c, {}, {})
        assert "iot · VLAN 30 · 192.168.30.0/23" in m["nets"]
    make_user(app, "admin")
    cli = new_client(app)
    do_login(cli, "admin")
    enable_2fa(cli, app, "admin")
    assert "iot · VLAN 30" in cli.get("/infrastruktura/adresy").text
