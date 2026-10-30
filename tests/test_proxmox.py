"""Adapter Proxmoxa i rdzeń integracji — na odpowiedziach API w formacie Proxmox VE (bez prawdziwego serwera)."""
import asyncio
import re

import httpx
import pytest
from sqlalchemy import select

from denali import discovery, integrations, inventory as inv
from denali.adapters import AdapterError
from denali.adapters.proxmox import Proxmox
from denali.models import integration, observed_state

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

CONFIG = {"url": "https://192.168.1.10:8006", "token_id": "denali@pve!odczyt", "verify_tls": True, "ca_pem": ""}
SECRETS = {"token_secret": "11111111-2222-3333-4444-555555555555"}

RESOURCES = [
    {"type": "node", "node": "pve", "status": "online", "uptime": 864000, "cpu": 0.1, "mem": 8 * 1024 ** 3, "maxmem": 32 * 1024 ** 3},
    {"type": "qemu", "vmid": 100, "name": "truenas", "node": "pve", "status": "running", "uptime": 3 * 86400,
     "mem": 6 * 1024 ** 3, "maxmem": 8 * 1024 ** 3, "template": 0},
    {"type": "qemu", "vmid": 107, "name": "docker", "node": "pve", "status": "running", "uptime": 100, "template": 0},
    {"type": "lxc", "vmid": 110, "name": "pihole", "node": "pve", "status": "stopped", "template": 0},
    {"type": "qemu", "vmid": 120, "name": "test", "node": "pve", "status": "stopped", "template": 0},
    {"type": "qemu", "vmid": 9000, "name": "debian-szablon", "node": "pve", "status": "stopped", "template": 1},
    {"type": "storage", "storage": "local", "node": "pve", "status": "available"},
]


def pve_api(resources=None, auth_ok=True, seen=None):
    resources = RESOURCES if resources is None else resources

    def handler(request):
        if seen is not None:
            seen.append(request)
        if not auth_ok:
            return httpx.Response(401, json={"data": None})
        path = request.url.path.removeprefix("/api2/json")
        routes = {
            "/version": {"version": "8.4.1"},
            "/cluster/resources": resources,
            "/cluster/status": [{"type": "cluster", "name": "dom"}, {"type": "node", "name": "pve", "ip": "192.168.1.10"}],
            "/nodes/pve/qemu/100/agent/network-get-interfaces": {"result": [
                {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
                {"name": "ens18", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "192.168.1.30"},
                                                   {"ip-address-type": "ipv6", "ip-address": "fe80::1"}]},
                {"name": "docker0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "172.17.0.1"}]}]},
            "/nodes/pve/lxc/110/config": {"onboot": 1, "hostname": "pihole"},
            "/nodes/pve/qemu/120/config": {"name": "test"},
        }
        if path in routes:
            return httpx.Response(200, json={"data": routes[path]})
        if path == "/nodes/pve/qemu/107/agent/network-get-interfaces":
            return httpx.Response(500, json={"data": None, "message": "QEMU guest agent is not running"})
        return httpx.Response(404, json={"data": None})
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── adapter ─────────────────────────
def test_odczyt_wezlow_vm_i_lxc():
    seen = []
    res = run(Proxmox(CONFIG, SECRETS, pve_api(seen=seen)).poll())
    found = {f.external_id: f.proposed for f in res.found}
    assert set(found) == {"node/pve", "qemu/100", "qemu/107", "lxc/110", "qemu/120"}      # bez szablonu i storage
    assert found["node/pve"] == {"name": "pve", "kind": "host", "addresses": [{"kind": "ip", "value": "192.168.1.10"}]}
    assert found["qemu/100"]["parent"] == {"ref": "node/pve"} and found["qemu/100"]["kind"] == "vm"
    assert found["qemu/100"]["addresses"] == [{"kind": "ip", "value": "192.168.1.30"}]   # bez lo i docker0
    assert found["qemu/107"]["addresses"] == []                                          # brak agenta QEMU
    assert found["lxc/110"]["kind"] == "lxc"
    states = {s.external_id: s for s in res.states}
    assert states["node/pve"].state == "ok" and "10 dni" in states["node/pve"].summary
    assert states["qemu/100"].state == "ok" and "RAM 6,0/8 GB" in states["qemu/100"].summary
    assert states["lxc/110"].state == "down"            # zatrzymany, a ma startować z hostem
    assert states["qemu/120"].state == "off"            # zatrzymana celowo — nie jest awarią
    assert seen[0].headers["authorization"] == "PVEAPIToken=denali@pve!odczyt=11111111-2222-3333-4444-555555555555"
    assert all(r.method == "GET" for r in seen)         # tylko odczyt


def test_sprawdzenie_polaczenia():
    msg = run(Proxmox(CONFIG, SECRETS, pve_api()).test())
    assert msg == "Połączono: Proxmox VE 8.4.1 · węzły: 1 · maszyny i kontenery: 4"
    with pytest.raises(AdapterError, match="odrzucił token"):
        run(Proxmox(CONFIG, SECRETS, pve_api(auth_ok=False)).test())
    with pytest.raises(AdapterError, match="PVEAuditor"):
        run(Proxmox(CONFIG, SECRETS, pve_api(resources=[])).test())


@pytest.mark.parametrize("config, msg", [
    ({**CONFIG, "url": "http://192.168.1.10:8006"}, "https"),
    ({**CONFIG, "url": "https://pve:8006/api2/json"}, "bez ścieżki"),
    ({**CONFIG, "token_id": "denali"}, "użytkownik@realm!nazwa"),
    ({**CONFIG, "url": ""}, "Uzupełnij"),
])
def test_walidacja_konfiguracji(config, msg):
    with pytest.raises(AdapterError, match=msg):
        Proxmox.validate(config, SECRETS)


# ───────────────────────── rdzeń integracji ─────────────────────────
def add(conn, app, **kw):
    return integrations.save(conn, app.state.box, "proxmox", "pve", {**CONFIG, **kw}, SECRETS)


def test_sekret_zaszyfrowany(conn, app):
    iid = add(conn, app)
    row = integrations.get(conn, iid)
    assert SECRETS["token_secret"] not in (row["secret_ct"] or "") and SECRETS["token_secret"] not in str(row["config"])
    assert integrations.secrets_of(row, app.state.box) == SECRETS


def test_odczyt_zasila_kolejke_i_stan(app):
    with app.state.engine.begin() as conn:
        iid = add(conn, app)
        row = integrations.get(conn, iid)
    assert run(integrations.poll_one(app.state.engine, app.state.box, row, pve_api(), now=1000)) is None
    with app.state.engine.begin() as conn:
        assert {r["external_id"] for r in discovery.pending(conn)} == {"node/pve", "qemu/100", "qemu/107", "lxc/110", "qemu/120"}
        states = {r.external_id: r.state for r in conn.execute(select(observed_state))}
        assert states["lxc/110"] == "down"
        assert integrations.get(conn, iid)["last_ok_at"] == 1000
        # zatwierdzenie VM 100 tworzy też węzeł pve, a stan widać na karcie
        ref = next(r for r in discovery.pending(conn) if r["external_id"] == "qemu/100")
        created = discovery.approve(conn, ref["id"])
        truenas = [d for d in created if inv.get_device(conn, d)["name"] == "truenas"][0]
        [src] = integrations.states_for_device(conn, truenas)
        assert src["state"] == "ok" and src["source"] == "pve"
    # VM 100 dostaje drugi adres, a VM 120 znika z Proxmoxa
    resources = [r for r in RESOURCES if r.get("vmid") != 120]
    with app.state.engine.begin() as conn:
        row = integrations.get(conn, iid)
    run(integrations.poll_one(app.state.engine, app.state.box, row, pve_api(resources=resources), now=2000))
    with app.state.engine.begin() as conn:
        states = {r.external_id: (r.state, r.summary) for r in conn.execute(select(observed_state))}
        assert states["qemu/120"] == ("unknown", "nie ma go już w źródle")


def test_blad_odczytu_zapisany_i_nie_zatrzymuje(app):
    with app.state.engine.begin() as conn:
        iid = add(conn, app)
        row = integrations.get(conn, iid)
    err = run(integrations.poll_one(app.state.engine, app.state.box, row, pve_api(auth_ok=False), now=500))
    assert "odrzucił token" in err
    with app.state.engine.begin() as conn:
        row = integrations.get(conn, iid)
        assert row["last_error"] == err and row["last_attempt_at"] == 500 and row["last_ok_at"] is None


def test_harmonogram(conn, app):
    iid = add(conn, app)
    assert [r["id"] for r in integrations.due(conn, now=100)] == [iid]          # nigdy nie czytana
    integrations.record_error(conn, iid, "x", now=100)
    assert integrations.due(conn, now=120) == []                               # co 60 s
    assert [r["id"] for r in integrations.due(conn, now=161)] == [iid]
    integrations.set_enabled(conn, iid, False)
    assert integrations.due(conn, now=1000) == []


# ───────────────────────── panel ─────────────────────────
def test_dodanie_przez_panel(app):
    app.state.adapter_transport = pve_api()
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        page = c.get("/admin/integracje/nowa?typ=proxmox")
        assert "Jak założyć token" in page.text
        r = c.post("/admin/integracje/nowa", data={"csrf_token": csrf_from(page.text), "type": "proxmox", "name": "pve",
                                                    "url": CONFIG["url"], "token_id": CONFIG["token_id"],
                                                    "token_secret": SECRETS["token_secret"], "verify_tls": "1",
                                                    "poll_seconds": "60"})
        assert r.status_code == 303
        view = c.get(r.headers["location"])
        assert "Połączono: Proxmox VE 8.4.1" in view.text
        assert SECRETS["token_secret"] not in view.text                       # sekret nigdy nie wraca do strony
        iid = re.search(r"/admin/integracje/(\d+)", r.headers["location"]).group(1)
        r = c.post(f"/admin/integracje/{iid}/odczyt", data={"csrf_token": csrf_from(view.text)})
        assert r.status_code == 200 and "truenas" in r.text and "do zatwierdzenia" in r.text
        assert "5" in c.get("/zatwierdz").text
        # zmiana bez podawania sekretu nie kasuje go
        r = c.post(f"/admin/integracje/{iid}", data={"csrf_token": csrf_from(view.text), "name": "pve",
                                                      "url": CONFIG["url"], "token_id": CONFIG["token_id"],
                                                      "token_secret": "", "poll_seconds": "120"})
        assert r.status_code == 303
    with app.state.engine.connect() as conn:
        row = conn.execute(select(integration)).mappings().first()
        assert integrations.secrets_of(row, app.state.box) == SECRETS and row["poll_seconds"] == 120


def test_podglad_nie_ma_dostepu_do_integracji(app):
    make_user(app, role="podglad", login="widz")
    with new_client(app) as c:
        do_login(c, login="widz")
        assert c.get("/admin/integracje").status_code == 403


# ───────────────────────── certyfikat CA ─────────────────────────
def make_ca_pem():
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Proxmox Virtual Environment")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_certyfikat_wklejony_z_konsoli_jest_czyszczony():
    from denali.adapters.proxmox import clean_pem
    pem = make_ca_pem()
    pasted = "root@pve:~# cat /etc/pve/pve-root-ca.pem\r\n" + pem.replace("\n", "\r\n") + "root@pve:~# "
    config = {**CONFIG, "ca_pem": pasted}
    Proxmox.validate(config, SECRETS)                      # nie rzuca, a zapisany certyfikat jest czysty
    assert config["ca_pem"] == clean_pem(pem) and config["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")


@pytest.mark.parametrize("pasted, msg", [
    ("to nie jest certyfikat", "nie wygląda poprawnie"),
    ("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----", "uszkodzony"),
])
def test_zly_certyfikat_odrzucony_przy_zapisie(pasted, msg):
    with pytest.raises(AdapterError, match=msg):
        Proxmox.validate({**CONFIG, "ca_pem": pasted}, SECRETS)


def test_zly_certyfikat_w_panelu_to_komunikat_a_nie_blad_serwera(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        page = c.get("/admin/integracje/nowa?typ=proxmox")
        r = c.post("/admin/integracje/nowa", data={"csrf_token": csrf_from(page.text), "type": "proxmox", "name": "pve",
                                                    "url": CONFIG["url"], "token_id": CONFIG["token_id"],
                                                    "token_secret": SECRETS["token_secret"], "verify_tls": "1",
                                                    "ca_pem": "root@pve:~# cat /etc/pve/pve-root-ca.pem", "poll_seconds": "60"})
        assert r.status_code == 400 and "Certyfikat CA nie wygląda poprawnie" in r.text
