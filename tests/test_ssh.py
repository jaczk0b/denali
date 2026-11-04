"""Terminal SSH: prawdziwy serwer asyncssh, klucz serwera, zamki i przekazywanie znaków przez panel."""
import asyncio
import json
import threading

import asyncssh
import pytest
from starlette.websockets import WebSocketDisconnect

from denali import actions, consoles, polecenia, ssh, vault
from denali import inventory as inv
from denali.adapters import AdapterError

from .conftest import csrf_from, do_login, make_user, new_client
from .test_akcje import admin, audit_rows, fresh_reauth, pve  # noqa: F401
from .test_terminale import session_row, ws_args

PASSWORD = "haslo-maszyny"
PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ1V1vTx2aV8vJ0m5a0G1Yl6YQmB7c4Vw0a7p4c1m0Yd"


SEEN = []                       # hasła, które dotarły do serwera


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        SEEN.append(password)
        return password == PASSWORD

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        return True


async def shell(process):
    sizes = process.get_terminal_size()
    process.stdout.write(f"witaj {process.get_extra_info('username')} {process.term_type} {sizes[0]}\r\n".encode())
    while True:
        try:
            data = await process.stdin.read(1024)
        except asyncssh.TerminalSizeChanged as e:
            process.stdout.write(f"resize:{e.width}\r\n".encode())
            continue
        if not data or data.strip() == b"exit":
            break
        process.stdout.write(b"echo:" + data)
    process.exit(0)


@pytest.fixture
def server():
    """Serwer SSH w osobnym wątku (ma własną pętlę, panel w testach działa w swojej)."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    def run():
        asyncio.set_event_loop(loop)

        async def start():
            srv = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key], process_factory=shell,
                                               encoding=None, line_editor=False)
            state["port"] = srv.get_port()
            ready.set()
        loop.run_until_complete(start())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    yield {"port": state["port"], "key": " ".join(key.export_public_key("openssh").decode().split()[:2])}
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def box(app, pve):
    """Urządzenie z adresem serwera testowego."""
    dev = pve["docker"][1]
    with app.state.engine.begin() as conn:
        inv.add_address(conn, dev, "ip", "127.0.0.1", primary=True)
    return dev


def run(coro):
    return asyncio.run(coro)


def open_ssh(app, client, dev, server, **fields):
    fresh_reauth(app, client)
    page = client.get(f"/terminale/{dev}").text
    return client.post(f"/terminale/{dev}/ssh", data={
        "csrf_token": csrf_from(page), "host": "127.0.0.1", "port": server["port"], "login": "jan", "password": PASSWORD, **fields})


def test_terminal_od_formularza_do_powloki(app, box, server, admin):
    r = open_ssh(app, admin, box, server)
    assert r.status_code == 303, r.text
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin.get(r.headers["location"])
    assert 'data-preset="df -h -x tmpfs -x devtmpfs"' in page.text
    assert "'unsafe-inline'" in page.headers["content-security-policy"]         # tylko tu, dla xterm.js
    assert PASSWORD not in page.text
    assert "unsafe-inline" not in admin.get("/terminale").headers["content-security-policy"]
    with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin)) as ws:
        assert b"witaj jan xterm-256color 120" in ws.receive_bytes()
        ws.send_text(json.dumps({"cols": 100, "rows": 30}))
        assert b"resize:100" in ws.receive_bytes()
        ws.send_bytes(b"ls -la\n")
        assert b"echo:ls -la" in ws.receive_bytes()
        ws.send_bytes(b"exit\n")
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_bytes()
    row = session_row(app, cid)
    assert row["closed_at"] and row["external_id"] == "ssh" and row["integration_id"] is None
    with app.state.engine.connect() as conn:
        [opened] = audit_rows(conn, "console_open")
        assert opened["params"]["login"] == "jan" and PASSWORD not in json.dumps(dict(opened["params"]))
        assert ssh.known_key(conn, "127.0.0.1", server["port"]) == server["key"]     # zapamiętany
    assert "ssh-screen" not in admin.get(f"/terminale/konsola/{cid}").text           # po końcu bez ponownego łączenia


def test_haslo_nie_zostaje_po_polaczeniu(app, box, server):
    t = ssh.target(app.state.engine, "127.0.0.1", server["port"], "jan", PASSWORD)

    async def scenario():
        s = await t.console_connect(None)
        await s.close()
    run(scenario())
    assert t.secret == ""


def test_zly_klucz_serwera_nie_dostaje_hasla(app, server):
    """Przypięty inny klucz: połączenie pada przed logowaniem, więc hasło nie wychodzi."""
    SEEN.clear()
    t = ssh.Target("127.0.0.1", server["port"], "jan", PASSWORD, PUBLIC, lambda k: None)
    with pytest.raises(AdapterError, match="Klucz serwera się zmienił"):
        run(t.console_connect(None))
    assert SEEN == []
    good = ssh.Target("127.0.0.1", server["port"], "jan", PASSWORD, server["key"], lambda k: pytest.fail("już znany"))

    async def scenario():
        s = await good.console_connect(None)
        await s.close()
    run(scenario())
    assert SEEN == [PASSWORD]


def test_zmieniony_klucz_i_zapomnienie(app, box, server, admin):
    with app.state.engine.begin() as conn:
        from denali.db import kv_set
        kv_set(conn, ssh.key_name("127.0.0.1", 22), PUBLIC)
    page = admin.get(f"/terminale/{box}").text
    assert "Zapomnij klucz" in page
    fresh_reauth(app, admin)
    r = admin.post(f"/terminale/{box}/ssh/zapomnij", data={"csrf_token": csrf_from(page), "host": "127.0.0.1"})
    assert r.status_code == 303
    with app.state.engine.connect() as conn:
        assert ssh.known_key(conn, "127.0.0.1", 22) is None


def test_zle_haslo_i_brak_serwera(app, server):
    t = ssh.target(app.state.engine, "127.0.0.1", server["port"], "jan", "zle")
    with pytest.raises(AdapterError, match="odrzucił logowanie"):
        run(t.console_connect(None))
    t = ssh.target(app.state.engine, "127.0.0.1", 1, "jan", "x")
    with pytest.raises(AdapterError, match="Nie da się połączyć"):
        run(t.console_connect(None))


def test_klucz_prywatny_zamiast_hasla(app, server):
    key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    t = ssh.target(app.state.engine, "127.0.0.1", server["port"], "jan", key)

    async def scenario():
        s = await t.console_connect(None)
        await s.close()
    run(scenario())
    with pytest.raises(AdapterError, match="nieczytelny"):
        run(ssh.target(app.state.engine, "127.0.0.1", server["port"], "jan", "-----BEGIN nie klucz").console_connect(None))


def test_haslo_ze_skarbca(app, box, server, admin):
    with app.state.engine.begin() as conn:
        sid = vault.add(conn, app.state.box, "root", PASSWORD, role="admin", user="admin", device_id=box, username="jan")
    assert "root (jan)" in admin.get(f"/terminale/{box}").text
    r = open_ssh(app, admin, box, server, login="", password="", secret_id=str(sid))
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    assert app.state.console_tickets[cid]["adapter"].login == "jan"
    with app.state.engine.connect() as conn:
        assert audit_rows(conn, "secret_reveal")


def test_wpis_ze_skarbca_innego_urzadzenia(app, box, pve, server, admin):
    with app.state.engine.begin() as conn:
        sid = vault.add(conn, app.state.box, "obcy", "x", role="admin", user="admin", device_id=pve["test"][1])
    r = open_ssh(app, admin, box, server, password="", secret_id=str(sid))
    assert r.status_code == 403 and "Skarbcu" in r.text


@pytest.mark.parametrize("fields, text", [
    ({"host": "10.9.9.9"}, "karcie urządzenia"), ({"port": "99999"}, "port"), ({"port": "abc"}, "port"),
    ({"login": "a b"}, "login"), ({"password": ""}, "hasło"),
])
def test_zamki_formularza(app, box, server, admin, fields, text):
    r = open_ssh(app, admin, box, server, **fields)
    assert r.status_code == 403 and text in r.text.lower()


def test_terminal_wymaga_hasla_i_wlaczonych_akcji(app, box, server, admin):
    page = admin.get(f"/terminale/{box}").text
    r = admin.post(f"/terminale/{box}/ssh", data={"csrf_token": csrf_from(page), "host": "127.0.0.1", "login": "jan", "password": "x"})
    assert r.headers["location"].startswith("/potwierdz")
    with app.state.engine.begin() as conn:
        actions.set_enabled(conn, False, "admin")
    assert open_ssh(app, admin, box, server).status_code == 403


def test_podglad_nie_ma_terminala(app, box, server):
    make_user(app, "widz", "bardzo-dlugie-haslo-widza", "podglad")
    with new_client(app) as c:
        do_login(c, "widz", "bardzo-dlugie-haslo-widza")
        assert c.get(f"/terminale/{box}").status_code == 403


def test_trwajaca_sesja_ssh_konczy_wylacznik(app, box, server, admin):
    r = open_ssh(app, admin, box, server)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    row = session_row(app, cid)
    token = admin.cookies.get("denali_session")
    with app.state.engine.begin() as conn:
        assert consoles.why_stop(conn, row, token, "") is None        # bez zakresu integracji: SSH go nie potrzebuje
        actions.set_enabled(conn, False, "admin")
        assert "wyłączono" in consoles.why_stop(conn, row, token, "").lower()


def test_lista_pokazuje_urzadzenia_z_adresem(app, box, admin):
    page = admin.get("/terminale").text
    assert f'href="/terminale/{box}"' in page and ">terminal<" in page


def test_wlasne_polecenia(app, box, admin):
    assert polecenia.parse_custom("Logi nginx | tail -f /var/log/nginx/error.log\n\n  uptime  ") == [
        {"label": "Logi nginx", "text": "tail -f /var/log/nginx/error.log"}, {"label": "uptime", "text": "uptime"}]
    page = admin.get("/terminale").text
    r = admin.post("/terminale/polecenia", data={"csrf_token": csrf_from(page), "presets": "Logi | journalctl -f"})
    assert r.status_code == 303
    assert "Logi | journalctl -f" in admin.get("/terminale").text
    with app.state.engine.connect() as conn:
        assert polecenia.presets(conn)[0] == {"label": "Logi", "text": "journalctl -f"}


def connect_once(app, client, cid):
    with client.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(client)) as ws:
        assert b"witaj" in ws.receive_bytes()
        ws.send_bytes(b"exit\n")
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_bytes()


def test_haslo_zapamietane_po_udanym_logowaniu_i_jeden_klik(app, box, server, admin):
    r = open_ssh(app, admin, box, server, remember="1")
    connect_once(app, admin, int(r.headers["location"].rsplit("/", 1)[1]))
    with app.state.engine.connect() as conn:
        [saved] = ssh.saved_logins(conn, box)
        assert saved["login"] == "jan" and saved["port"] == server["port"]
        assert audit_rows(conn, "secret_create")
    # lista i strona urządzenia mają przycisk, a klik wysyła tylko numer wpisu
    assert "Otwórz terminal: jan" in admin.get(f"/terminale/{box}").text
    page = admin.get("/terminale").text
    assert '<button class="btn small primary" type="submit">Terminal</button>' in page
    assert PASSWORD not in page
    fresh_reauth(app, admin)
    r = admin.post(f"/terminale/{box}/ssh", data={"csrf_token": csrf_from(page), "host": "127.0.0.1",
                                                  "port": saved["port"], "secret_id": saved["id"]})
    assert r.status_code == 303
    connect_once(app, admin, int(r.headers["location"].rsplit("/", 1)[1]))


def test_zle_haslo_nie_trafia_do_skarbca(app, box, server, admin):
    r = open_ssh(app, admin, box, server, remember="1", password="zle")
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"wss://testserver/terminale/konsola/{cid}/ws", **ws_args(admin)) as ws:
            ws.receive_bytes()
    with app.state.engine.connect() as conn:
        assert ssh.saved_logins(conn, box) == []


def test_bez_zaznaczenia_nic_nie_zostaje(app, box, server, admin):
    r = open_ssh(app, admin, box, server)
    connect_once(app, admin, int(r.headers["location"].rsplit("/", 1)[1]))
    with app.state.engine.connect() as conn:
        assert ssh.saved_logins(conn, box) == []


def test_nowe_haslo_zastepuje_stare(app, box, pve):
    with app.state.engine.begin() as conn:
        ssh.remember_login(conn, app.state.box, box, "jan", 22, "stare", "admin")
        ssh.remember_login(conn, app.state.box, box, "jan", 2222, "nowe", "admin")
        [saved] = ssh.saved_logins(conn, box)
        assert saved["port"] == 2222
        row = conn.execute(vault.vault_secret.select().where(vault.vault_secret.c.id == saved["id"])).mappings().first()
        assert app.state.box.decrypt(row["ciphertext"]) == "nowe"
