"""TrueNAS: pule ZFS, zadania snapshotów i replikacji, alerty TrueNAS. Tylko odczyt (klucz API).

Od 25.04 właściwym API jest JSON-RPC 2.0 po WebSocket (wss://host/api/current); REST v2.0 jest przestarzały,
ale w starszych wersjach jedyny — próbujemy najpierw nowego, potem starego (tak samo jak HLC).
Pule, snapshoty i replikacje nie są osobnymi urządzeniami — podpinają się pod kartę TrueNAS.
"""
import json
import socket
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from . import Adapter, AdapterError, Field, Found, PollResult, State
from .tls import ssl_context, validate_pem_field

CA_HINT = "certyfikat z TrueNAS: Credentials → Certificates → truenas_default → pobierz"
REST = {"system.info": "/system/info", "pool.query": "/pool", "pool.snapshottask.query": "/pool/snapshottask",
        "replication.query": "/replication", "alert.list": "/alert/list", "sharing.nfs.query": "/sharing/nfs"}
POOL_DOWN = {"FAULTED", "UNAVAIL", "OFFLINE", "REMOVED"}
USAGE_WARN = 0.90
ALERT_LEVELS = {"CRITICAL", "ERROR", "ALERT", "EMERGENCY"}


def _ts(v):
    if isinstance(v, dict) and "$date" in v:
        return v["$date"] / 1000
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "nigdy"


class Truenas(Adapter):
    type = "truenas"
    label = "TrueNAS"
    default_poll = 120
    fields = [
        Field("url", "Adres", "url", help="np. https://192.168.1.30 — adres panelu TrueNAS"),
        Field("api_key", "Klucz API", "secret"),
        Field("ca_pem", "Certyfikat TrueNAS", "textarea", required=False,
              help="Certyfikat, którego używa panel TrueNAS (domyślnie truenas_default)."),
        Field("verify_tls", "Sprawdzaj certyfikat", "bool", required=False, default=True),
    ]
    ws_connect = None        # w testach: podmiana połączenia WebSocket

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip().rstrip("/"))
        if p.scheme not in ("http", "https") or not p.hostname or p.path or p.query:
            raise AdapterError("Adres: https://IP-albo-nazwa (adres panelu TrueNAS), bez ścieżki.")
        validate_pem_field(config, CA_HINT)

    def _ssl(self):
        return ssl_context(self.config.get("ca_pem", ""), bool(self.config.get("verify_tls", True)), CA_HINT)

    async def _fetch(self, methods):
        """{metoda: wynik albo None}. Najpierw JSON-RPC po WebSocket, gdy go nie ma — REST."""
        if not self.transport:
            try:
                return await self._fetch_ws(methods)
            except LookupError:
                pass                                   # starszy TrueNAS bez /api/current
        return await self._fetch_rest(methods)

    async def _fetch_ws(self, methods):
        import websockets
        u = urlsplit(str(self.config["url"]).strip())
        scheme = "wss" if u.scheme == "https" else "ws"
        connect = self.ws_connect or websockets.connect
        kw = {"ssl": self._ssl()} if scheme == "wss" else {}
        try:
            ws = await connect(f"{scheme}://{u.netloc}/api/current", open_timeout=15, **kw)
        except websockets.InvalidStatus as e:
            if getattr(e.response, "status_code", 0) in (404, 403, 400):
                raise LookupError from None
            raise AdapterError(f"TrueNAS odrzucił połączenie WebSocket ({e.response.status_code}).") from None
        except (OSError, websockets.InvalidHandshake, TimeoutError) as e:
            if "CERTIFICATE" in str(e).upper():
                raise AdapterError("Certyfikat TrueNAS nie jest zaufany — wklej go albo wyłącz sprawdzanie.") from None
            raise AdapterError(f"Brak połączenia z {self.config['url']} — sprawdź adres i czy TrueNAS działa.") from None
        try:
            seq = 0

            async def call(method, params=None):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params or []}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq:
                        if "error" in msg:
                            raise AdapterError(f"TrueNAS {method}: {msg['error'].get('message', msg['error'])}")
                        return msg.get("result")

            if await call("auth.login_with_api_key", [self.secrets["api_key"].strip()]) is not True:
                raise AdapterError("TrueNAS odrzucił klucz API.")
            out = {}
            for m in methods:
                try:
                    out[m] = await call(m)
                except AdapterError:
                    out[m] = None                      # metoda nieznana w tej wersji — pomijamy
            return out
        finally:
            await ws.close()

    async def _fetch_rest(self, methods):
        base = str(self.config["url"]).strip().rstrip("/") + "/api/v2.0"
        kw = {"transport": self.transport} if self.transport else {"verify": self._ssl()}
        out = {}
        async with httpx.AsyncClient(base_url=base, timeout=20, follow_redirects=False,
                                     headers={"Authorization": f"Bearer {self.secrets['api_key'].strip()}"}, **kw) as cli:
            for m in methods:
                try:
                    r = await cli.get(REST[m])
                except httpx.ConnectError:
                    raise AdapterError(f"Brak połączenia z {self.config['url']} — sprawdź adres.") from None
                except httpx.HTTPError as e:
                    raise AdapterError(f"Błąd połączenia: {e.__class__.__name__}") from None
                if r.status_code == 401:
                    raise AdapterError("TrueNAS odrzucił klucz API.")
                out[m] = r.json() if r.status_code < 400 else None
        return out

    async def test(self):
        data = await self._fetch(["system.info", "pool.query"])
        info = data.get("system.info") or {}
        return f"Połączono: TrueNAS {info.get('version', '?')} · pule: {len(data.get('pool.query') or [])}"

    async def poll(self):
        data = await self._fetch(list(REST))
        info = data.get("system.info") or {}
        out = PollResult()
        host = urlsplit(str(self.config["url"])).hostname or "truenas"
        try:
            socket.inet_aton(host)
            addrs = [{"kind": "ip", "value": host}]
        except OSError:
            addrs = []
        out.found.append(Found("server", {"name": info.get("hostname") or host, "kind": "nas", "addresses": addrs}))
        alerts = [a for a in data.get("alert.list") or []
                  if not a.get("dismissed") and str(a.get("level", "")).upper() in ALERT_LEVELS]
        texts = [str(a.get("formatted") or a.get("text") or "alert")[:300] for a in alerts]
        server = {"version": info.get("version"), "alerts": len(alerts), "alert_texts": texts[:20],
                  "nfs": [x.get("path") or (x.get("paths") or [""])[0] for x in data.get("sharing.nfs.query") or []
                          if x.get("enabled", True)]}
        if alerts:
            out.states.append(State("server", "warn", f"alerty TrueNAS: {len(alerts)} — {texts[0][:150]}", server))
        else:
            out.states.append(State("server", "ok", f"TrueNAS {info.get('version', '')} działa".replace("  ", " "),
                                    server))
        for p in data.get("pool.query") or []:
            name, status = p.get("name"), str(p.get("status") or "UNKNOWN").upper()
            if not name:
                continue
            size, alloc = p.get("size") or 0, p.get("allocated") or 0
            pct = alloc / size if size else None
            if status in POOL_DOWN:
                state = "down"
            elif status != "ONLINE" or p.get("healthy") is False or (pct is not None and pct >= USAGE_WARN):
                state = "warn"
            else:
                state = "ok"
            summary = f"pula {name}: {status}" + (f" · zajęte {pct:.0%}" if pct is not None else "")
            out.states.append(State(f"pool/{name}", state, summary, {"status": status, "used": pct, "size": size, "allocated": alloc},
                                    attach={"ref": "server"}, label=f"pula {name}"))
        for kind, method, label in (("snapshot", "pool.snapshottask.query", "snapshoty"),
                                    ("replication", "replication.query", "replikacja")):
            for t in data.get(method) or []:
                if t.get("enabled") is False:
                    continue
                key = t.get("dataset") if kind == "snapshot" else t.get("name") or t.get("id")
                st = t.get("state") or {}
                last = _ts(st.get("datetime"))
                s = str(st.get("state") or "").upper()
                if s in ("ERROR", "FAILED"):
                    state, summary = "warn", f"{label} {key}: błąd — {str(st.get('error') or '')[:120]}"
                elif s in ("FINISHED", "SUCCESS"):
                    state, summary = "ok", f"{label} {key}: ostatnio {_when(last)}"
                else:
                    state, summary = "unknown", f"{label} {key}: {s.lower() or 'jeszcze nie uruchomione'}"
                out.states.append(State(f"{kind}/{key}", state, summary, {"last": last, "state": s},
                                        attach={"ref": "server"}, label=f"{label} {key}"))
        return out
