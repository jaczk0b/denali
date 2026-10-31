"""Sprawdzenia z LAN: co jest sprawdzane (samo, bez konfiguracji), ping z zapasowym TCP, HTTP, certyfikat, zapis."""
import asyncio
import socket
import time

import httpx
import pytest
from sqlalchemy import select

from denali import checks, inventory as inv, profiles
from denali.models import check_state

from .conftest import do_login, enable_2fa, make_user, new_client


def run(coro):
    return asyncio.run(coro)


def test_plan_bez_konfiguracji(conn):
    pve = inv.create_device(conn, "pve", "host")
    inv.add_address(conn, pve, "ip", "192.168.1.10", primary=True)
    nas = inv.create_device(conn, "nas", "nas")
    inv.add_address(conn, nas, "hostname", "nas.lan")
    v6 = inv.create_device(conn, "v6", "host")
    inv.add_address(conn, v6, "ip", "fe80::1")
    app_ = inv.create_device(conn, "Immich", "kontener", app_url="https://immich.lan")
    off = inv.create_device(conn, "stary", "host")
    inv.add_address(conn, off, "ip", "192.168.1.99")
    profiles.set_override(conn, off, "monitor", False)
    specs = set(checks.plan(conn))
    assert specs == {checks.Spec(pve, "ping", "192.168.1.10"), checks.Spec(nas, "ping", "nas.lan"),
                     checks.Spec(app_, "http", "https://immich.lan")}


def test_ping_icmp_i_zapasowy_tcp(monkeypatch):
    monkeypatch.setattr(checks, "icmp_ping", lambda ip, timeout=2: 3.4)
    r = run(checks.ping("192.168.1.10"))
    assert r.ok and r.summary == "ping 3 ms"

    def no_icmp(ip, timeout=2):
        raise PermissionError
    monkeypatch.setattr(checks, "icmp_ping", no_icmp)
    monkeypatch.setattr(checks, "tcp_alive", lambda ip, timeout=2: (5.0, 22))
    assert run(checks.ping("192.168.1.10")).summary == "odpowiada (port 22, 5 ms)"
    monkeypatch.setattr(checks, "tcp_alive", lambda ip, timeout=2: (None, None))
    r = run(checks.ping("192.168.1.10"))
    assert not r.ok and "nie odpowiada" in r.summary


def test_tcp_odmowa_polaczenia_znaczy_ze_host_zyje(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    free_port = srv.getsockname()[1]
    srv.close()                                         # nikt nie słucha → odmowa połączenia
    monkeypatch.setattr(checks, "TCP_PORTS", (free_port,))
    ms, port = checks.tcp_alive("127.0.0.1")
    assert ms is not None and port == free_port


@pytest.mark.parametrize("status, ok", [(200, True), (302, True), (401, True), (503, False)])
def test_http(status, ok, monkeypatch):
    monkeypatch.setattr(checks, "cert_expiry", lambda host, port=443, timeout=5: None)
    def handler(req):          # 302 → strona logowania (jak w prawdziwej aplikacji)
        if status == 302 and req.url.path != "/login":
            return httpx.Response(302, headers={"location": "/login"})
        return httpx.Response(200 if status == 302 else status)
    tr = httpx.MockTransport(handler)
    r = run(checks.http("http://app.lan", tr))
    assert r.ok is ok


def test_http_brak_polaczenia():
    def boom(req):
        raise httpx.ConnectError("refused")
    r = run(checks.http("http://app.lan", httpx.MockTransport(boom)))
    assert not r.ok and "brak połączenia" in r.summary


def test_certyfikat(monkeypatch):
    now = 1_800_000_000
    tr = httpx.MockTransport(lambda req: httpx.Response(200))
    monkeypatch.setattr(checks, "cert_expiry", lambda host, port=443, timeout=5: now + 60 * 86400)
    r = run(checks.http("https://app.lan", tr, now=now))
    assert r.ok and not r.warn and "certyfikat do" in r.summary
    monkeypatch.setattr(checks, "cert_expiry", lambda host, port=443, timeout=5: now + 5 * 86400)
    r = run(checks.http("https://app.lan", tr, now=now))
    assert r.ok and r.warn and "certyfikat wygasa" in r.summary
    monkeypatch.setattr(checks, "cert_expiry", lambda host, port=443, timeout=5: now - 86400)
    r = run(checks.http("https://app.lan", tr, now=now))
    assert not r.ok and "certyfikat wygasł" in r.summary


def test_zapis_pamieta_od_kiedy_nie_dziala(conn):
    d = inv.create_device(conn, "pve", "host")
    spec = checks.Spec(d, "ping", "192.168.1.10")
    checks.record(conn, [(spec, checks.Result(True, "ping 1 ms", 1.0))], [spec], now=100)
    checks.record(conn, [(spec, checks.Result(False, "nie odpowiada"))], [spec], now=160)
    checks.record(conn, [(spec, checks.Result(False, "nie odpowiada"))], [spec], now=220)
    row = conn.execute(select(check_state)).mappings().first()
    assert row["first_fail_at"] == 160 and row["last_ok_at"] == 100 and not row["ok"]
    checks.record(conn, [(spec, checks.Result(True, "ping 1 ms", 1.0))], [spec], now=280)
    row = conn.execute(select(check_state)).mappings().first()
    assert row["first_fail_at"] is None and row["ok"]
    checks.record(conn, [], [], now=300)                # adres usunięty → sprawdzenie znika
    assert conn.execute(select(check_state)).first() is None


def test_pelny_obieg_i_karta(app, monkeypatch):
    monkeypatch.setattr(checks, "icmp_ping", lambda ip, timeout=2: 2.0)
    with app.state.engine.begin() as c:
        d = inv.create_device(c, "pve", "host")
        inv.add_address(c, d, "ip", "192.168.1.10")
    assert run(checks.run(app.state.engine, now=time.time())) == 1
    make_user(app)
    with new_client(app) as cl:
        do_login(cl)
        enable_2fa(cl, app)
        html = cl.get(f"/urzadzenia/{d}").text
    assert "Ping z LAN" in html and "ping 2 ms" in html


# ───────────────────────── z internetu ─────────────────────────
PUBLIC_IP = "203.0.113.7"       # adres VPS-a z Pangolinem (tu: z puli dokumentacyjnej, w teście udajemy publiczny)


def internet_transport(answers, app_status=302, dns_down=(), seen=None):
    """Publiczny DNS (DoH) i aplikacja za Pangolinem — bez sieci."""
    def handler(request):
        if request.url.path in ("/dns-query", "/resolve"):
            if request.url.host in dns_down:
                raise httpx.ConnectError("brak sieci")
            name = request.url.params["name"]
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": ip} for ip in answers.get(name, [])]})
        if seen is not None:
            seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(app_status, headers={"location": "https://auth.example.com/"})
    return httpx.MockTransport(handler)


@pytest.fixture
def vps_is_public(monkeypatch):
    real = checks._public_ip
    monkeypatch.setattr(checks, "_public_ip", lambda ip: ip == PUBLIC_IP or real(ip))


def test_co_moze_byc_publiczne(conn):
    for url, expected in [("https://immich.serwispowers.com", True), ("http://ha.example.org:8123/", True),
                          ("https://immich.lan", False), ("https://192.168.1.40", False), ("http://nas", False),
                          ("https://nas.home.arpa", False), ("ftp://pliki.example.org", False)]:
        assert checks.public_candidate(url) is expected, url
    d = inv.create_device(conn, "Immich", "kontener", app_url="https://immich.serwispowers.com")
    assert set(checks.plan(conn)) == {checks.Spec(d, "http", "https://immich.serwispowers.com"),
                                      checks.Spec(d, "internet", "https://immich.serwispowers.com")}


def test_z_internetu_przez_publiczny_dns(vps_is_public):
    seen = []
    t = internet_transport({"immich.serwispowers.com": [PUBLIC_IP]}, seen=seen)
    r = run(checks.internet("https://immich.serwispowers.com/foto?x=1", t))
    assert r.ok and r.summary.startswith("z internetu: HTTP 302")          # logowanie Pangolina = droga działa
    # łączymy się z adresem z publicznego DNS, z prawdziwą nazwą (Host i SNI — certyfikat jest sprawdzany)
    assert seen == [(f"https://{PUBLIC_IP}/foto?x=1", "immich.serwispowers.com", "immich.serwispowers.com")]
    r = run(checks.internet("https://immich.serwispowers.com", internet_transport(
        {"immich.serwispowers.com": [PUBLIC_IP]}, app_status=502)))
    assert not r.ok and "502" in r.summary and "Pangolin" in r.summary


def test_z_internetu_tylko_lan_i_brak_internetu(vps_is_public):
    # rekord z adresem prywatnym (szara chmurka) albo brak rekordu = aplikacja tylko w LAN → nie sprawdzamy
    for answers in ({"denali.serwispowers.com": ["192.168.1.5"]}, {}):
        r = run(checks.internet("https://denali.serwispowers.com", internet_transport(answers)))
        assert r.skip
    # pierwszy DNS nie odpowiada → drugi
    r = run(checks.internet("https://immich.serwispowers.com", internet_transport(
        {"immich.serwispowers.com": [PUBLIC_IP]}, dns_down=("1.1.1.1",))))
    assert r.ok
    # żaden DNS nie odpowiada → w domu nie ma internetu; to nie jest awaria aplikacji
    r = run(checks.internet("https://immich.serwispowers.com", internet_transport(
        {}, dns_down=("1.1.1.1", "8.8.8.8"))))
    assert r.unknown and not r.skip


def test_z_internetu_w_stanie_urzadzenia(conn, vps_is_public):
    from denali import live
    d = inv.create_device(conn, "Immich", "kontener", app_url="https://immich.serwispowers.com")
    lan = checks.Spec(d, "http", "https://immich.serwispowers.com")
    net = checks.Spec(d, "internet", "https://immich.serwispowers.com")
    ok_lan = (lan, checks.Result(True, "HTTP 200"))
    down_after = profiles.value(conn, d, "down_after_s")

    checks.record(conn, [ok_lan, (net, checks.Result(False, "z internetu: błąd 502"))], [lan, net], now=1000)
    checks.record(conn, [ok_lan, (net, checks.Result(False, "z internetu: błąd 502"))], [lan, net],
                  now=1000 + down_after)
    s = live.compute(conn, now=1000 + down_after)[d]
    assert s["state"] == "warn" and "502" in s["why"]                       # „uwaga”, nie awaria i nie incydent

    checks.record(conn, [ok_lan, (net, checks.Result(False, "brak internetu w domu", unknown=True))], [lan, net],
                  now=5000)
    row = conn.execute(select(check_state).where(check_state.c.kind == "internet")).mappings().first()
    assert row["first_fail_at"] is None
    assert live.compute(conn, now=5000 + down_after)[d]["state"] == "ok"   # LAN działa, z internetu: nie wiadomo

    checks.record(conn, [ok_lan, (net, checks.Result(False, "adres tylko w LAN", skip=True))], [lan, net], now=6000)
    assert [r.kind for r in conn.execute(select(check_state))] == ["http"]  # rekord zmieniony na LAN → znika
