"""Terminal SSH z karty Terminale: prawdziwa powłoka w xterm.js, dla każdego urządzenia z adresem.

Panel łączy się z maszyną jak zwykły klient SSH i przekazuje znaki. Login i hasło (albo klucz prywatny) podaje
administrator przy otwarciu, a po udanym logowaniu trafiają do Skarbca urządzenia, więc następnym razem wystarczy kliknąć.
Poza Skarbcem nic nie zostaje, a w pamięci hasło żyje tylko do połączenia.

Klucz panelu: jeden klucz na wszystkie maszyny, trzymany zaszyfrowany w ustawieniach. Przy logowaniu z zaznaczonym „Dodaj
klucz panelu” trafia do authorized_keys, a do Skarbca idzie sam znacznik PANEL_KEY zamiast hasła — kolejne logowania
przechodzą tą samą drogą co zapisane hasło. Maszyna bez zwykłej powłoki po prostu zostaje przy haśle.

Klucz serwera: przy pierwszym połączeniu panel go zapamiętuje, potem przyjmuje tylko ten sam (jak known_hosts). Zgodność
sprawdza klient przed wysłaniem hasła, więc podstawiony serwer go nie dostanie.
"""
import asyncio
import json

import asyncssh

from . import vault
from .adapters import AdapterError
from .db import kv_get, kv_set

EXTERNAL_ID = "ssh"     # console_session.external_id: sesja SSH, bez integracji
LABEL = "SSH "          # początek nazwy wpisu w Skarbcu, z którego terminal otwiera się jednym kliknięciem
CONNECT_TIMEOUT = 10
KEEPALIVE = 30
PANEL_KEY = "klucz panelu Denali"   # treść wpisu w Skarbcu: logowanie kluczem panelu, bez hasła
PANEL_KV = "ssh.panel_key"
INSTALL_TIMEOUT = 10


def panel_key(conn, box):
    """Klucz prywatny panelu w formacie OpenSSH. Powstaje przy pierwszym użyciu."""
    stored = kv_get(conn, PANEL_KV)
    if stored:
        return box.decrypt(stored)
    private = asyncssh.generate_private_key("ssh-ed25519", comment="denali").export_private_key("openssh").decode()
    kv_set(conn, PANEL_KV, box.encrypt(private))
    return private


def panel_public(conn, box):
    return asyncssh.import_private_key(panel_key(conn, box)).export_public_key("openssh").decode().strip()


def install_command(public):
    """Dopisuje klucz raz; pusta linia chroni przed plikiem bez znaku nowej linii na końcu."""
    return ("umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF '{public}' ~/.ssh/authorized_keys || printf '\\n%s\\n' '{public}' >> ~/.ssh/authorized_keys)")


def saved_logins(conn, device_id):
    """Zapamiętane logowania urządzenia: [{"id", "login", "port"}] — wpisy Skarbca założone przez terminal."""
    rows = vault.list_secrets(conn, role="admin", device_id=device_id)
    return [{"id": r["id"], "login": r["username"], "port": int(r["note"]) if r["note"].isdigit() else 22}
            for r in rows if r["label"].startswith(LABEL) and r["username"]]


def remember_login(conn, box, device_id, login, port, secret, user, ip=""):
    """Wpis w Skarbcu: nowy albo z nowym hasłem, gdy ten login już tam jest."""
    found = next((s for s in saved_logins(conn, device_id) if s["login"] == login), None)
    if found:
        vault.update(conn, box, found["id"], role="admin", user=user, ip=ip, secret=secret, note=str(port))
    else:
        vault.add(conn, box, LABEL + login, secret, role="admin", user=user, ip=ip, device_id=device_id, username=login,
                  note=str(port))


def key_name(host, port):
    return f"ssh.hostkey.{host}:{port}"


def known_key(conn, host, port):
    return kv_get(conn, key_name(host, port))


class Session:
    """Jedna powłoka w kształcie, jakiego oczekuje consoles.bridge: bajty z maszyny, send() z przeglądarki.
    Tekstowa ramka z przeglądarki to nowy rozmiar okna ({"cols", "rows"}), binarna to naciśnięte klawisze."""
    close_code = None
    ended_text = "Maszyna zakończyła sesję"

    def __init__(self, conn, process):
        self.conn, self.process = conn, process

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.process.stdout.read(65536)
        if not data:
            raise StopAsyncIteration
        return data

    async def send(self, data):
        if isinstance(data, bytes):
            self.process.stdin.write(data)
            return
        size = json.loads(data)
        self.process.change_terminal_size(int(size["cols"]), int(size["rows"]))

    async def close(self):
        self.conn.close()


class Target:
    """Dokąd i z czym się łączyć. Hasło jest tu tylko do chwili połączenia."""

    def __init__(self, host, port, login, secret, pinned, remember, logged_in=None, install=""):
        self.host, self.port, self.login, self.secret = host, port, login, secret
        self.pinned, self.remember = pinned, remember        # remember(klucz): zapamiętanie klucza nowego serwera
        self.logged_in = logged_in                           # logged_in(hasło): po udanym logowaniu — zapis do Skarbca
        self.install = install                               # klucz publiczny panelu do dopisania na maszynie

    def _auth(self):
        if self.secret.lstrip().startswith("-----BEGIN"):
            try:
                return {"client_keys": [asyncssh.import_private_key(self.secret)], "password": None}
            except (asyncssh.KeyImportError, ValueError):
                raise AdapterError("Klucz prywatny jest nieczytelny albo ma hasło.") from None
        return {"password": self.secret, "client_keys": None}

    async def _add_key(self, conn):
        try:
            result = await asyncio.wait_for(conn.run(install_command(self.install), check=False), INSTALL_TIMEOUT)
        except (asyncssh.Error, OSError, TimeoutError):
            return False
        return result.exit_status == 0

    async def console_connect(self, _info):
        secret = self.secret
        known = asyncssh.import_known_hosts(f"* {self.pinned}\n") if self.pinned else None
        try:
            conn = await asyncssh.connect(
                self.host, self.port, username=self.login, known_hosts=known, connect_timeout=CONNECT_TIMEOUT,
                keepalive_interval=KEEPALIVE, agent_path=None, config=None, **self._auth())
            process = await conn.create_process(term_type="xterm-256color", term_size=(120, 32), encoding=None)
        except asyncssh.HostKeyNotVerifiable:
            raise AdapterError("Klucz serwera się zmienił. Jeśli maszyna była instalowana od nowa, użyj „Zapomnij klucz” "
                               "na stronie urządzenia. Inaczej ktoś może się pod nią podszywać.") from None
        except asyncssh.PermissionDenied:
            raise AdapterError("Serwer odrzucił logowanie: zły login lub hasło, brak klucza panelu na maszynie albo "
                               "logowanie hasłem jest wyłączone (np. PermitRootLogin dla roota).") from None
        except (OSError, TimeoutError):
            raise AdapterError(f"Nie da się połączyć z {self.host}:{self.port} — czy działa tam SSH?") from None
        except asyncssh.Error as e:
            raise AdapterError(f"SSH: {e.reason}"[:200]) from None
        finally:
            self.secret = ""
        if not self.pinned:
            self.remember(" ".join(conn.get_server_host_key().export_public_key("openssh").decode().split()[:2]))
        if self.install and await self._add_key(conn):
            secret = PANEL_KEY
        if self.logged_in:
            self.logged_in(secret)
        return Session(conn, process)


def target(engine, host, port, login, secret, logged_in=None, install=""):
    def remember(key):
        with engine.begin() as conn:
            kv_set(conn, key_name(host, port), key)

    with engine.connect() as conn:
        return Target(host, port, login, secret, known_key(conn, host, port), remember, logged_in, install)
