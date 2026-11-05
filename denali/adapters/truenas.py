"""TrueNAS: pule ZFS, dyski (temperatura, testy SMART), zadania snapshotów i replikacji, alerty TrueNAS.
Tylko odczyt (klucz API).

Od 25.04 właściwym API jest JSON-RPC 2.0 po WebSocket (wss://host/api/current); REST v2.0 jest przestarzały,
ale w starszych wersjach jedyny — próbujemy najpierw nowego, potem starego (tak samo jak HLC).
Pule, dyski, snapshoty i replikacje nie są osobnymi urządzeniami — podpinają się pod kartę TrueNAS.
Alerty: lista wszystkich nieodrzuconych (jak dzwonek w TrueNAS); stan „uwaga” dają tylko poważne (ERROR i wyżej).
Dysk: „uwaga”, gdy ostatni test SMART się nie udał albo dysk jest za gorący. Temperatury i wyniki SMART czytamy
tym, co TrueNAS już ma (bez budzenia uśpionych dysków); metoda nieznana w danej wersji = brak tych danych.
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
        "replication.query": "/replication", "alert.list": "/alert/list", "sharing.nfs.query": "/sharing/nfs",
        "disk.query": "/disk", "smart.test.results": "/smart/test/results"}
WS_ONLY = ("disk.temperatures",)     # w REST to POST — tam bez temperatur
POOL_DOWN = {"FAULTED", "UNAVAIL", "OFFLINE", "REMOVED"}
USAGE_WARN = 0.90
USAGE_CRIT = 0.95
ALERT_LEVELS = {"CRITICAL", "ERROR", "ALERT", "EMERGENCY"}
LEVEL_ORDER = ("EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO")
ALERTS_MAX = 50
HOT = {"HDD": 55, "SSD": 70}         # °C — powyżej dysk dostaje „uwaga” (NVMe i nieznany typ liczą się jak SSD)
SMART_BAD = {"FAILED", "FAILURE", "ERROR"}


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


def _alerts(raw):
    """Wszystkie nieodrzucone alerty, od najpoważniejszych i najnowszych."""
    out = []
    for a in raw or []:
        if not isinstance(a, dict) or a.get("dismissed"):
            continue
        level = str(a.get("level") or "INFO").upper()
        out.append({"level": level, "text": str(a.get("formatted") or a.get("text") or "alert")[:300],
                    "klass": str(a.get("klass") or "")[:60], "at": _ts(a.get("datetime"))})
    out.sort(key=lambda a: (LEVEL_ORDER.index(a["level"]) if a["level"] in LEVEL_ORDER else 9, -(a["at"] or 0)))
    return out[:ALERTS_MAX]


def _pool_disks(pools):
    """{nazwa dysku: pula} z topologii pul (vdevy i ich dyski)."""
    out = {}

    def walk(node, pool):
        if isinstance(node, dict):
            if node.get("disk"):
                out[str(node["disk"])] = pool
            for child in node.get("children") or []:
                walk(child, pool)
        elif isinstance(node, list):
            for x in node:
                walk(x, pool)

    for p in pools or []:
        if isinstance(p.get("topology"), dict):
            for group in p["topology"].values():
                walk(group, p.get("name"))
    return out


def _temp(v):
    if isinstance(v, dict):
        v = v.get("temp", v.get("temperature"))
    return v if isinstance(v, (int, float)) else None


def _smart(results):
    """{dysk: ostatni test SMART} z smart.test.results (pierwszy na liście = najnowszy)."""
    out = {}
    for r in results or []:
        if not isinstance(r, dict) or not r.get("disk"):
            continue
        tests = [t for t in r.get("tests") or [] if isinstance(t, dict)]
        if tests:
            t = tests[0]
            out[str(r["disk"])] = {"desc": str(t.get("description") or "test")[:60],
                                   "status": str(t.get("status") or "").upper(),
                                   "verbose": str(t.get("status_verbose") or "")[:120],
                                   "lifetime": t.get("lifetime")}
    return out


def _disk_states(data):
    disks = [d for d in data.get("disk.query") or [] if isinstance(d, dict) and d.get("name")]
    temps = data.get("disk.temperatures") if isinstance(data.get("disk.temperatures"), dict) else {}
    smart, pools = _smart(data.get("smart.test.results")), _pool_disks(data.get("pool.query"))
    out = []
    for d in sorted(disks, key=lambda d: (pools.get(d["name"]) or "~", str(d["name"]))):
        name = str(d["name"])
        kind = str(d.get("type") or "").upper()
        temp = _temp(temps.get(name))
        test = smart.get(name)
        problems = []
        if test and test["status"] in SMART_BAD:
            problems.append(f"test SMART nieudany ({test['verbose'] or test['status'].lower()})")
        if temp is not None and temp >= HOT.get(kind, HOT["SSD"]):
            problems.append(f"za gorący: {temp:.0f} °C")
        parts = [f"dysk {name}", f"pula {pools[name]}" if pools.get(name) else "poza pulą"]
        if temp is not None:
            parts.append(f"{temp:.0f} °C")
        if test:
            parts.append(f"SMART: {test['desc']} — {test['verbose'] or test['status'].lower()}")
        summary = f"dysk {name}: " + ", ".join(problems) if problems else " · ".join(parts)
        serial = str(d.get("serial") or "").strip()
        out.append(State(f"disk/{serial or name}", "warn" if problems else "ok", summary[:300], {
            "name": name, "model": str(d.get("model") or "")[:80], "serial": serial[:60], "size": d.get("size"),
            "type": kind, "pool": pools.get(name), "temp": temp, "smart": test, "problems": problems,
            "alarms": ([{"rule": "smart_failed", "key": serial or name, "text": f"Dysk {name}: {problems[0]}"}]
                       if test and test["status"] in SMART_BAD else [])
            + ([{"rule": "disk_hot", "key": serial or name, "text": f"Dysk {name}: {temp:.0f} °C"}]
               if temp is not None and temp >= HOT.get(kind, HOT["SSD"]) else [])},
            attach={"ref": "server"}, label=f"dysk {name}"))
    return out


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
                if m not in REST:
                    out[m] = None
                    continue
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
        data = await self._fetch(list(REST) + list(WS_ONLY))
        info = data.get("system.info") or {}
        out = PollResult()
        host = urlsplit(str(self.config["url"])).hostname or "truenas"
        try:
            socket.inet_aton(host)
            addrs = [{"kind": "ip", "value": host}]
        except OSError:
            addrs = []
        out.found.append(Found("server", {"name": info.get("hostname") or host, "kind": "nas", "addresses": addrs}))
        all_alerts = _alerts(data.get("alert.list"))
        alerts = [a for a in all_alerts if a["level"] in ALERT_LEVELS]
        texts = [a["text"] for a in alerts]
        server = {"version": info.get("version"), "alerts": len(alerts), "alert_texts": texts[:20],
                  "alert_list": all_alerts, "disks_read": data.get("disk.query") is not None,
                  "smart_read": data.get("smart.test.results") is not None,
                  "nfs": [x.get("path") or (x.get("paths") or [""])[0] for x in data.get("sharing.nfs.query") or []
                          if x.get("enabled", True)],
                  "alarms": [{"rule": "truenas_alert", "key": a["klass"] or a["text"][:80], "text": a["text"],
                              "severity": "krytyczny" if a["level"] in ("CRITICAL", "ALERT", "EMERGENCY") else "ostrzezenie"}
                             for a in alerts]}
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
            alarms = []
            if status not in POOL_DOWN and (status != "ONLINE" or p.get("healthy") is False):
                alarms.append({"rule": "pool_unhealthy", "key": name, "text": f"Pula {name}: {status}"})
            if pct is not None and pct >= USAGE_WARN:
                alarms.append({"rule": "storage", "key": name, "text": f"Pula {name} zajęta w {pct:.0%}",
                               "severity": "krytyczny" if pct >= USAGE_CRIT else "ostrzezenie"})
            out.states.append(State(f"pool/{name}", state, summary, {"status": status, "used": pct, "size": size, "allocated": alloc,
                                                                     "alarms": alarms},
                                    attach={"ref": "server"}, label=f"pula {name}"))
        out.states += _disk_states(data)
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
                out.states.append(State(f"{kind}/{key}", state, summary,
                                        {"last": last, "state": s,
                                         "alarms": [{"rule": "backup_task", "key": str(key), "text": summary}]
                                         if state == "warn" else []},
                                        attach={"ref": "server"}, label=f"{label} {key}"))
        return out
