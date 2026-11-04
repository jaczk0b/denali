"""Karta Terminale (Etap 6): konsola przez API Proxmoxa, przekazywanie WebSocket, zamki, gotowe polecenia przez agenta."""
import asyncio
import datetime
import ipaddress
import json
import shlex
import ssl

import httpx
import pytest
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from denali import actions, consoles, integrations, polecenia
from denali.adapters import AdapterError
from denali.adapters.proxmox import Proxmox
from denali.models import action_run

from .conftest import csrf_from, do_login, make_user, new_client
from .test_agent import agent
from .test_akcje import admin, agent_on_vm, audit_rows, fresh_reauth, pve, req, run  # noqa: F401
from .test_proxmox import CONFIG, SECRETS


def ticket_api(seen=None, deny=False):
    """Proxmox: vncproxy daje bilet (albo 403, gdy token nie ma VM.Console)."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        assert request.url.path.endswith("/vncproxy")
        if deny:
            return httpx.Response(403, json={"data": None})
        return httpx.Response(200, json={"data": {"port": "5900", "ticket": "haslo123:PVEVNC:bilet", "password": "haslo123",
                                                  "upid": "UPID:x"}})
    return httpx.MockTransport(handler)


class FakeConsole:
    """Konsola w Proxmoxie: oddaje ramki z listy, zapisuje to, co przyszło z przeglądarki, i kończy na „koniec”."""
    def __init__(self, frames=()):
        self.frames, self.sent, self.closed = list(frames), [], False
        self.finish = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.frames:
            return self.frames.pop(0)
        self.finish = self.finish or asyncio.Event()
        await self.finish.wait()
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)
        if data == b"koniec" and self.finish:
            self.finish.set()

    async def close(self):
        self.closed = True


@pytest.fixture
def console(app, pve, monkeypatch):
    """Zakres nadany, zamiast połączenia z Proxmoxem — FakeConsole."""
    with app.state.engine.begin() as conn:
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["akcje", "konsola"], "admin")
    app.state.adapter_transport = ticket_api()
    fake = FakeConsole([b"RFB 003.008\n"])

    async def connect(self, info):
        fake.info = info
        return fake
    monkeypatch.setattr(Proxmox, "console_connect", connect)
    return fake


def open_page(app, client, pve):
    """Od formularza do strony konsoli; zwraca id sesji i token CSRF."""
    ref_id, dev = pve["docker"]
    fresh_reauth(app, client)
    html = client.get(f"/terminale/{dev}").text
    assert "Otwórz konsolę" in html, html
    r = client.post(f"/terminale/{dev}/konsola/{ref_id}", data={"csrf_token": csrf_from(html)})
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1]), csrf_from(html)


def start(client, cid, csrf):
    return client.post(f"/terminale/konsola/{cid}/start", headers={"X-CSRF-Token": csrf})


def ws_args(client, origin="https://testserver"):
    headers = {"cookie": f"denali_session={client.cookies.get('denali_session')}"}
    if origin:
        headers["origin"] = origin
    return {"headers": headers}


def session_row(app, cid):
    with app.state.engine.connect() as conn:
        return consoles.get(conn, cid)


# ───────────────────────── konsola ─────────────────────────
def test_konsola_od_formularza_do_zakonczenia(app, pve, console, admin):
    cid, csrf = open_page(app, admin, pve)
    assert 'data-ws="/terminale/konsola/%d/ws"' % cid in admin.get(f"/terminale/konsola/{cid}").text
    r = start(admin, cid, csrf)
    assert r.json() == {"password": "haslo123"}
    with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin)) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"       # z Proxmoxa do przeglądarki
        ws.send_bytes(b"z przegladarki")
        ws.send_bytes(b"koniec")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert console.sent == [b"z przegladarki", b"koniec"] and console.closed
    assert console.info == {"path": "/nodes/pve/qemu/107", "port": "5900", "ticket": "haslo123:PVEVNC:bilet",
                            "password": "haslo123"}
    row = session_row(app, cid)
    assert row["closed_at"] and row["close_reason"] == "Proxmox zakończył konsolę."
    with app.state.engine.connect() as conn:
        assert [a["params"]["session"] for a in audit_rows(conn, "console_open")] == [cid]
        [closed] = audit_rows(conn, "console_close")
    assert closed["result"] == "Proxmox zakończył konsolę."
    assert "Proxmox zakończył konsolę." in admin.get(f"/terminale/konsola/{cid}").text    # bez ponownego łączenia


def test_konsola_wymaga_kolejnych_zamkow(app, pve, console, admin):
    ref_id, dev = pve["docker"]
    html = admin.get(f"/terminale/{dev}").text
    url = f"/terminale/{dev}/konsola/{ref_id}"
    assert admin.post(url, data={"csrf_token": csrf_from(html)}).headers["location"].startswith("/potwierdz")

    def refused(text, **kw):
        with app.state.engine.begin() as conn, pytest.raises(consoles.ConsoleError, match=text):
            consoles.request(conn, device_id=dev, ref_id=ref_id, user="admin",
                             **{"role": "admin", "reauth_ok": True, **kw})

    refused("potwierdzenia", reauth_ok=False)
    refused("administrator", role="viewer")
    with app.state.engine.begin() as conn:
        actions.set_enabled(conn, False, "admin")
    refused("wyłączone")
    with app.state.engine.begin() as conn:
        actions.set_enabled(conn, True, "admin")
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["akcje"], "admin")
    refused("zakresu „Konsola”")
    with app.state.engine.connect() as conn:
        assert len(audit_rows(conn, "console_blocked")) >= 4


def test_wylaczona_maszyna_nie_ma_konsoli(app, pve, console):
    ref_id, dev = pve["test"]        # VM 120 jest wyłączona
    with app.state.engine.begin() as conn:
        with pytest.raises(consoles.ConsoleError, match="stanie"):
            consoles.request(conn, device_id=dev, ref_id=ref_id, user="admin", role="admin", reauth_ok=True)


def test_konsola_nie_jest_zwykla_akcja(app, pve, console, admin):
    ref_id, dev = pve["docker"]
    with app.state.engine.connect() as conn:
        assert not [o for o in actions.for_device(conn, dev) if o["key"] == "proxmox.console"]
        assert "proxmox.console" not in [k for k, _s, _c in actions.known_for_device(conn, dev)]   # poza „Dozwolonymi”
    assert admin.get(f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.console").status_code == 404


@pytest.mark.parametrize("origin", ["https://zla.strona", ""])
def test_websocket_odrzuca_obcy_origin(app, pve, console, admin, origin):
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin, origin)):
            pass
    assert cid in app.state.console_tickets             # odmowa nie zużywa biletu ani nie łączy z Proxmoxem
    assert not hasattr(console, "info")


def test_websocket_bez_logowania(app, pve, console, admin):
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    with new_client(app) as anon, pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws",
                                    headers={"origin": "https://testserver"}):
            pass
    assert cid in app.state.console_tickets


def test_bilet_jest_jednorazowy(app, pve, console, admin):
    cid, csrf = open_page(app, admin, pve)
    assert start(admin, cid, csrf).status_code == 200
    r = start(admin, cid, csrf)
    assert r.status_code == 409 and "skończyła" in r.json()["error"]
    app.state.console_tickets.clear()
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin)):
            pass


def test_cudza_sesja_konsoli(app, pve, console, admin):
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    make_user(app, "drugi", "bardzo-dlugie-haslo-drugiego", "podglad")
    with new_client(app) as other:
        do_login(other, "drugi", "bardzo-dlugie-haslo-drugiego")
        assert other.get(f"/terminale/konsola/{cid}").status_code in (403, 404)
        assert other.get("/terminale").status_code == 403
        assert 'href="/terminale"' not in other.get("/").text
        with pytest.raises(WebSocketDisconnect):
            with other.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(other)):
                pass


def test_zla_rola_tokenu_konczy_sesje(app, pve, console, admin):
    app.state.adapter_transport = ticket_api(deny=True)
    cid, csrf = open_page(app, admin, pve)
    r = start(admin, cid, csrf)
    assert r.status_code == 502 and "Uprawnienia" in r.json()["error"]
    assert "Uprawnienia" in session_row(app, cid)["close_reason"]


def test_kiedy_trwajaca_sesja_ma_sie_zamknac(app, pve, console, admin):
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    token = admin.cookies.get("denali_session")
    row = session_row(app, cid)
    with app.state.engine.begin() as conn:
        assert consoles.why_stop(conn, row, token, "") is None
        assert "2 godziny" in consoles.why_stop(conn, row, token, "", now=row["opened_at"] + consoles.MAX_LIFE + 1)
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["akcje"], "admin")
        assert "Konsola" in consoles.why_stop(conn, row, token, "")
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["konsola"], "admin")
        actions.set_enabled(conn, False, "admin")
        assert "wyłączono" in consoles.why_stop(conn, row, token, "").lower()
        actions.set_enabled(conn, True, "admin")
        assert "Logowanie" in consoles.why_stop(conn, row, "zly-token", "")


def test_most_zamyka_gdy_sprawdzenie_poda_powod(monkeypatch):
    monkeypatch.setattr(consoles, "CHECK_EVERY", 0)

    class Browser:
        async def receive(self):
            await asyncio.Event().wait()

    fake = FakeConsole()
    assert run(consoles.bridge(Browser(), fake, lambda: "Akcje wyłączono.")) == "Akcje wyłączono."
    assert fake.closed


def test_bilet_bez_przegladarki_konczy_sesje(app, pve, console, admin):
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    app.state.console_tickets[cid]["at"] -= consoles.TICKET_VALID + 1
    consoles.forget_unused(app)
    assert cid not in app.state.console_tickets
    assert "Websockets Support" in session_row(app, cid)["close_reason"]


def test_koniec_sesji_podaje_powod(app, pve, console, admin, monkeypatch):
    # przeglądarka zgłasza koniec, zanim WebSocket dotarł do panelu (NPM bez Websockets Support)
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    assert "Websockets Support" in admin.get(f"/terminale/konsola/{cid}/koniec").json()["reason"]
    assert cid not in app.state.console_tickets

    async def refused(self, info):
        raise AdapterError("Proxmox odrzucił połączenie z konsolą (HTTP 401)")
    monkeypatch.setattr(Proxmox, "console_connect", refused)
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin)) as ws:
            ws.receive_bytes()
    assert "HTTP 401" in admin.get(f"/terminale/konsola/{cid}/koniec").json()["reason"]


def test_nieoczekiwany_blad_polaczenia_zostaje_w_historii(app, pve, console, admin, monkeypatch):
    async def broken(self, info):
        raise ValueError("zły adres")
    monkeypatch.setattr(Proxmox, "console_connect", broken)
    cid, csrf = open_page(app, admin, pve)
    start(admin, cid, csrf)
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin)) as ws:
            ws.receive_bytes()
    assert "ValueError: zły adres" in session_row(app, cid)["close_reason"]


def test_most_podaje_kod_rozlaczenia_przegladarki():
    class Browser:
        async def receive(self):
            return {"type": "websocket.disconnect", "code": 1006}

    assert run(consoles.bridge(Browser(), FakeConsole(), lambda: None)) == "Przeglądarka straciła połączenie (kod 1006)."


def test_most_podaje_kod_zamkniecia_proxmoxa():
    class Browser:
        async def receive(self):
            await asyncio.Event().wait()

    fake = FakeConsole()
    fake.close_code, fake.close_reason = 1011, "vnc"

    async def scenario():
        fake.finish = asyncio.Event()
        fake.finish.set()
        return await consoles.bridge(Browser(), fake, lambda: None)
    assert run(scenario()) == "Proxmox zakończył konsolę (kod 1011: vnc)."


def test_proxmox_vncproxy_jednym_tokenem():
    seen = []
    a = Proxmox(CONFIG, SECRETS, transport=ticket_api(seen))
    info = run(a.console_ticket("lxc/105", {"node": "pve"}))
    assert info == {"path": "/nodes/pve/lxc/105", "port": "5900", "ticket": "haslo123:PVEVNC:bilet", "password": "haslo123"}
    assert seen[0].headers["authorization"] == f"PVEAPIToken={CONFIG['token_id']}={SECRETS['token_secret']}"
    assert seen[0].content == b"websocket=1"
    with pytest.raises(AdapterError, match="węźle"):
        run(a.console_ticket("qemu/1", {}))
    with pytest.raises(AdapterError, match="Uprawnienia"):
        run(Proxmox(CONFIG, SECRETS, transport=ticket_api(deny=True)).console_ticket("lxc/105", {"node": "pve"}))


def tls_server_context(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "pve")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    pem = tmp_path / "pve.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pem)
    return ctx


def test_polaczenie_z_vncwebsocket_po_tls(tmp_path):
    """Prawdziwy WebSocket po TLS: adres, bilet, nagłówek tokenu i podprotokół, jakich chce Proxmox."""
    seen = {}

    async def handler(ws):
        seen.update(path=ws.request.path, auth=ws.request.headers["Authorization"], sub=ws.subprotocol)
        await ws.send(b"RFB 003.008\n")
        seen["got"] = await ws.recv()

    async def scenario():
        async with websockets.serve(handler, "127.0.0.1", 0, ssl=tls_server_context(tmp_path),
                                    subprotocols=["binary"]) as srv:
            port = srv.sockets[0].getsockname()[1]
            a = Proxmox({**CONFIG, "url": f"https://127.0.0.1:{port}", "verify_tls": False}, SECRETS)
            up = await a.console_connect({"path": "/nodes/pve/qemu/107", "port": "5900", "ticket": "PVEVNC:a+b/c=="})
            assert await up.recv() == b"RFB 003.008\n"
            await up.send(b"odpowiedz")
            await asyncio.sleep(0.05)
            await up.close()

    run(scenario())
    assert seen["path"] == "/api2/json/nodes/pve/qemu/107/vncwebsocket?port=5900&vncticket=PVEVNC%3Aa%2Bb%2Fc%3D%3D"
    assert seen["auth"] == f"PVEAPIToken={CONFIG['token_id']}={SECRETS['token_secret']}" and seen["sub"] == "binary"
    assert seen["got"] == b"odpowiedz"


@pytest.mark.parametrize("status, text", [(401, "HTTP 401"), (200, "IP-węzła:8006")])
def test_odmowa_vncwebsocket_ma_czytelny_blad(tmp_path, status, text):
    async def scenario():
        def refuse(_conn, _request):
            return websockets.Response(status, "x", websockets.Headers({"Server": "openresty"}), b"")

        async with websockets.serve(lambda ws: None, "127.0.0.1", 0, ssl=tls_server_context(tmp_path),
                                    process_request=refuse) as srv:
            port = srv.sockets[0].getsockname()[1]
            a = Proxmox({**CONFIG, "url": f"https://127.0.0.1:{port}", "verify_tls": False}, SECRETS)
            with pytest.raises(AdapterError, match=text):
                await a.console_connect({"path": "/nodes/pve/qemu/107", "port": "5900", "ticket": "x"})

    run(scenario())


def test_terminale_nie_zaleza_od_profili(app, pve, console, admin):
    """Zakres w integracji wystarcza: VM ma konsolę bez wpisu w profilu, także wyłączona jest na liście."""
    page = admin.get("/terminale").text
    assert "docker" in page and "Otwórz" in page
    assert "Otwórz konsolę" in admin.get(f"/terminale/{pve['docker'][1]}").text
    assert "Konsola" not in admin.get("/admin/profile").text
    with app.state.engine.begin() as conn:
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), [], "admin")
    empty = admin.get("/terminale").text
    assert "nie jest zapisana" in empty and f"/admin/integracje/{pve['iid']}#uprawnienia" in empty


def test_terminale_w_dolnym_pasku(app, pve, console, admin):
    home = admin.get("/").text
    assert '<nav class="dock"' in home and home.count('href="/terminale"') == 1
    assert 'href="/terminale"' in admin.get("/incydenty").text                  # górne menu na pozostałych stronach
    page = admin.get("/terminale").text
    assert "docker" in page


# ───────────────────────── gotowe polecenia ─────────────────────────
def test_lista_polecen_zgadza_sie_z_agentem():
    assert {k: shlex.split(c.text) for k, c in polecenia.COMMANDS.items()} == agent.COMMANDS
    assert agent.RESULT_MAX == polecenia.OUTPUT_MAX


def test_polecenie_od_panelu_do_wyniku(app, pve, monkeypatch, admin):
    iid, target, report = agent_on_vm(app, pve, monkeypatch, local=("polecenia",))
    ref_id, dev = target
    html = admin.get(f"/terminale/{dev}").text
    assert "Miejsce na dyskach" in html
    r = admin.post(f"/terminale/{dev}/polecenie", data={"csrf_token": csrf_from(html), "ref_id": ref_id, "command": "dyski"})
    assert r.status_code == 303
    with app.state.engine.begin() as conn:
        rid, status, params = conn.execute(select(action_run.c.id, action_run.c.status, action_run.c.params)).one()
        assert status == "zlecona" and params == {"command": "dyski"}
        row = integrations.get(conn, iid)
        jobs, _ack = actions.agent_exchange(conn, row, [])
        assert jobs == [{"id": rid, "action": "polecenia", "params": {"command": "dyski"}}]
        long = "x" * 15000
        actions.agent_exchange(conn, row, [{"id": rid, "state": "ok", "output": long}])
    page = admin.get(f"/terminale/{dev}").text
    assert long in page
    assert "Miejsce na dyskach" in page


def test_polecenie_spoza_listy_i_zwykla_droga(app, pve, monkeypatch, admin):
    _iid, target, _r = agent_on_vm(app, pve, monkeypatch, local=("polecenia",))
    ref_id, dev = target
    html = admin.get(f"/terminale/{dev}").text
    r = admin.post(f"/terminale/{dev}/polecenie", data={"csrf_token": csrf_from(html), "ref_id": ref_id,
                                                        "command": "rm -rf /"})
    assert r.status_code == 403 and "Nie ma takiego polecenia" in r.text
    assert admin.post(f"/urzadzenia/{dev}/akcje/{ref_id}/agent.polecenia",
                      data={"csrf_token": csrf_from(html)}).status_code == 403     # nie obok zwykłych akcji
    with app.state.engine.connect() as conn:
        assert conn.execute(select(action_run)).first() is None
        assert len(audit_rows(conn, "action_blocked")) == 2


def test_polecenie_bez_lokalnej_zgody_agenta(app, pve, monkeypatch, admin):
    _iid, target, _r = agent_on_vm(app, pve, monkeypatch, local=())
    assert "pozwol polecenia" in admin.get(f"/terminale/{target[1]}").text


def test_polecenia_nie_czekaja_na_siebie(app, pve, monkeypatch):
    _iid, target, _r = agent_on_vm(app, pve, monkeypatch, local=("polecenia",))
    with app.state.engine.begin() as conn:
        for key in ("dyski", "pamiec"):
            req(conn, target, "agent.polecenia", command=key)
        assert len(conn.execute(select(action_run)).all()) == 2


def test_agent_wykonuje_tylko_polecenia_ze_swojej_listy(tmp_path, monkeypatch):
    jobs, results = tmp_path / "zlecenia", tmp_path / "wyniki"
    jobs.mkdir()
    results.mkdir()
    agent.write_jobs([{"id": 1, "action": "polecenia", "params": {"command": "pamiec"}},
                      {"id": 2, "action": "polecenia", "params": {"command": "rm -rf /", "argv": ["rm"]}}],
                     str(jobs), str(results))
    assert json.loads((jobs / "1.json").read_text()) == {"id": 1, "action": "polecenia", "params": {"command": "pamiec"}}
    assert json.loads((jobs / "2.json").read_text()) == {"id": 2, "action": "polecenia"}     # obce parametry odpadają
    calls = []
    monkeypatch.setattr(agent, "run", lambda argv, timeout=30, env=None: (calls.append(argv), (0, "y" * 25000, ""))[1])
    agent.do_jobs({"actions": ["polecenia"]}, str(jobs), str(results))
    assert calls == [["free", "-h"]]
    res = {r["id"]: r for r in agent.read_results(str(results))}
    assert res[1]["state"] == "ok" and len(res[1]["output"]) == agent.RESULT_MAX
    assert res[2]["state"] == "blad"
