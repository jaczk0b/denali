"""Komodo: serwery (periphery) i stacki Dockera. Tylko odczyt (API `POST /read`, klucz użytkownika bez prawa zapisu).

Serwer Komodo to zwykle VM z Proxmoxa — zgłaszamy go z adresem IP, a kolejka „do zatwierdzenia” podpowie
przypięcie do istniejącej VM. Stack „działa na” swoim serwerze.
Stan stacka: running → działa; unhealthy/restarting → uwaga; down/dead → nie działa;
stopped/paused/created → wyłączony celowo.
Aktualizacje: usługa stacka z `update_available` (Komodo samo sprawdza, czy w rejestrze jest nowszy obraz tego samego
tagu — „Poll for updates” w ustawieniach stacka) = pozycja na liście aktualizacji.
"""
import socket
from urllib.parse import urlsplit

import httpx

from . import Adapter, AdapterError, Field, Found, PollResult, State, Update

STACK_STATE = {
    "running": "ok", "deploying": "ok",
    "unhealthy": "warn", "restarting": "warn",
    "down": "down", "dead": "down", "removing": "warn",
    "stopped": "off", "paused": "off", "created": "off",
}
STACK_LABEL = {
    "running": "działa", "deploying": "wdrażanie", "unhealthy": "kontener nie przechodzi kontroli zdrowia",
    "restarting": "kontener się restartuje", "down": "nie działa", "dead": "kontener martwy",
    "removing": "usuwanie", "stopped": "zatrzymany", "paused": "wstrzymany", "created": "utworzony, nie uruchomiony",
}


def _host_ip(address):
    """Adres periphery (np. https://192.168.1.20:8120) → IPv4, jeśli da się ustalić."""
    host = urlsplit(address if "://" in (address or "") else f"https://{address}").hostname
    if not host:
        return None
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        try:
            return socket.gethostbyname(host)
        except OSError:
            return None


class Komodo(Adapter):
    type = "komodo"
    label = "Komodo"
    default_poll = 60
    fields = [
        Field("url", "Adres Komodo", "url", help="np. https://komodo.lan albo http://192.168.1.20:9120"),
        Field("api_key", "Klucz API (Key)"),
        Field("api_secret", "Sekret klucza (Secret)", "secret"),
        Field("verify_tls", "Sprawdzaj certyfikat", "bool", required=False, default=True),
    ]

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip())
        if p.scheme not in ("http", "https") or not p.hostname or p.query:
            raise AdapterError("Adres: http(s)://adres-komodo[:port].")

    def _client(self):
        base = str(self.config["url"]).strip().rstrip("/")
        headers = {"X-Api-Key": self.config["api_key"].strip(), "X-Api-Secret": self.secrets["api_secret"].strip(),
                   "Content-Type": "application/json"}
        kw = {"transport": self.transport} if self.transport else {"verify": bool(self.config.get("verify_tls", True))}
        return httpx.AsyncClient(base_url=base, headers=headers, timeout=15, follow_redirects=False, **kw)

    async def _read(self, cli, typ, params=None):
        try:
            r = await cli.post("/read", json={"type": typ, "params": params or {}})
        except httpx.ConnectError:
            raise AdapterError(f"Brak połączenia z {self.config['url']} — sprawdź adres i czy Komodo działa.") from None
        except httpx.TimeoutException:
            raise AdapterError(f"{self.config['url']} nie odpowiada (przekroczony czas).") from None
        except httpx.HTTPError as e:
            raise AdapterError(f"Błąd połączenia: {e.__class__.__name__}") from None
        if r.status_code in (401, 403):
            raise AdapterError("Komodo odrzuciło klucz API — sprawdź Key i Secret.")
        if r.status_code >= 400:
            raise AdapterError(f"Komodo zwróciło błąd {r.status_code} dla {typ}.")
        try:
            return r.json()
        except ValueError:
            raise AdapterError("Odpowiedź nie wygląda na API Komodo — sprawdź adres (adres panelu, nie periphery).") from None

    async def test(self):
        async with self._client() as cli:
            try:
                ver = (await self._read(cli, "GetVersion") or {}).get("version", "?")
            except AdapterError:
                ver = "?"
            servers = await self._read(cli, "ListServers") or []
            stacks = await self._read(cli, "ListStacks") or []
        return f"Połączono: Komodo {ver} · serwery: {len(servers)} · stacki: {len(stacks)}"

    async def poll(self):
        async with self._client() as cli:
            servers = await self._read(cli, "ListServers") or []
            stacks = await self._read(cli, "ListStacks") or []
        out = PollResult(updates=[])
        for s in servers:
            sid, info = s.get("id"), s.get("info") or {}
            if not sid:
                continue
            eid = f"server/{sid}"
            ip = _host_ip(info.get("address") or "")
            out.found.append(Found(eid, {"name": s.get("name") or eid, "kind": "vm",
                                         "addresses": [{"kind": "ip", "value": ip}] if ip else []}))
            st = str(info.get("state") or "")
            state, summary = {"Ok": ("ok", "Komodo łączy się z serwerem"),
                              "NotOk": ("down", "Komodo nie łączy się z serwerem (periphery)"),
                              "Disabled": ("off", "serwer wyłączony w Komodo")}.get(st, ("unknown", f"stan: {st or '?'}"))
            out.states.append(State(eid, state, summary, {"komodo_state": st, "address": info.get("address")}))
        for s in stacks:
            sid, info = s.get("id"), s.get("info") or {}
            if not sid:
                continue
            eid = f"stack/{sid}"
            proposed = {"name": s.get("name") or eid, "kind": "kontener"}
            if info.get("server_id"):
                proposed["parent"] = {"ref": f"server/{info['server_id']}"}
            out.found.append(Found(eid, proposed))
            st = str(info.get("state") or "unknown").lower()
            services = info.get("services") or []
            updates = [x.get("service") for x in services if isinstance(x, dict) and x.get("update_available")]
            out.updates += [Update(eid, str(x.get("service") or "?"), "obraz", current=str(x.get("image") or ""))
                            for x in services if isinstance(x, dict) and x.get("update_available")]
            summary = f"stack {STACK_LABEL.get(st, st)}"
            if services:
                summary += f" · usługi: {len(services)}"
            if updates:
                summary += f" · aktualizacja dostępna: {', '.join(str(u) for u in updates[:3])}"
            out.states.append(State(eid, STACK_STATE.get(st, "unknown"), summary,
                                    {"komodo_state": st, "services": len(services), "updates": updates}))
        return out
