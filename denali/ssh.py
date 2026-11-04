"""Terminal SSH z karty Terminale: prawdziwa powłoka w xterm.js, dla każdego urządzenia z adresem.

Panel łączy się z maszyną jak zwykły klient SSH i przekazuje znaki. Login i hasło (albo klucz prywatny) podaje
administrator przy otwarciu albo wybiera wpis ze Skarbca — poza Skarbcem nic nie zostaje, a hasło żyje tylko do połączenia.

Klucz serwera: przy pierwszym połączeniu panel go zapamiętuje, potem przyjmuje tylko ten sam (jak known_hosts). Zgodność
sprawdza klient przed wysłaniem hasła, więc podstawiony serwer go nie dostanie.
"""
import json

import asyncssh

from .adapters import AdapterError
from .db import kv_get, kv_set

EXTERNAL_ID = "ssh"     # console_session.external_id: sesja SSH, bez integracji
CONNECT_TIMEOUT = 10
KEEPALIVE = 30


def key_name(host, port):
    return f"ssh.hostkey.{host}:{port}"


def known_key(conn, host, port):
    return kv_get(conn, key_name(host, port))


class Session:
    """Jedna powłoka w kształcie, jakiego oczekuje consoles.bridge: bajty z maszyny, send() z przeglądarki.
    Tekstowa ramka z przeglądarki to nowy rozmiar okna ({"cols", "rows"}), binarna to naciśnięte klawisze."""
    close_code = None

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

    def __init__(self, host, port, login, secret, pinned, remember):
        self.host, self.port, self.login, self.secret = host, port, login, secret
        self.pinned, self.remember = pinned, remember        # remember(klucz): zapamiętanie klucza nowego serwera

    def _auth(self):
        if self.secret.lstrip().startswith("-----BEGIN"):
            try:
                return {"client_keys": [asyncssh.import_private_key(self.secret)], "password": None}
            except (asyncssh.KeyImportError, ValueError):
                raise AdapterError("Klucz prywatny jest nieczytelny albo ma hasło.") from None
        return {"password": self.secret, "client_keys": None}

    async def console_connect(self, _info):
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
            raise AdapterError("Serwer odrzucił logowanie: zły login lub hasło albo logowanie hasłem jest wyłączone "
                               "(np. PermitRootLogin dla roota).") from None
        except (OSError, TimeoutError):
            raise AdapterError(f"Nie da się połączyć z {self.host}:{self.port} — czy działa tam SSH?") from None
        except asyncssh.Error as e:
            raise AdapterError(f"SSH: {e.reason}"[:200]) from None
        finally:
            self.secret = ""
        if not self.pinned:
            self.remember(" ".join(conn.get_server_host_key().export_public_key("openssh").decode().split()[:2]))
        return Session(conn, process)


def target(engine, host, port, login, secret):
    def remember(key):
        with engine.begin() as conn:
            kv_set(conn, key_name(host, port), key)

    with engine.connect() as conn:
        return Target(host, port, login, secret, known_key(conn, host, port), remember)
