"""Router albo AP na OpenWrt — przez ubus (JSON-RPC na /ubus, to samo API, którego używa LuCI).

Odczyt kontem rpcd z ACL tylko na potrzebne obiekty (instrukcja i skrypt na stronie integracji):
- system: model, wersja OpenWrt, czas pracy, obciążenie, pamięć,
- network.interface / network.device: WAN (adres, od kiedy działa), porty (link, prędkość),
- iwinfo: sieci Wi-Fi i skojarzeni klienci z sygnałem,
- plik /tmp/dhcp.leases i `ip -4 neigh`: kto jest w sieci (nazwa z DHCP, obecność z tablicy sąsiadów),
- `opkg list-upgradable` albo `apk list --upgradable` (OpenWrt 25+): pakiety do aktualizacji. Listy pakietów
  odświeża cron na routerze (`opkg update` / `apk update` raz na dobę) — router bez tego zgłosi pustą listę.

Jedyna akcja: kopia konfiguracji (`sysupgrade -b`, przez cgi-backup jak w LuCI) — OSOBNYM kontem z prawem tylko do
niej, bo archiwum zawiera hasła Wi-Fi i skróty haseł kont. Denali zapisuje je zaszyfrowane przy urządzeniu.

Klienci jak przy TP-Linku: kolejka „do zatwierdzenia”, dopasowanie po MAC, zależność „sieć” od routera / AP
(Wi-Fi twarda, kabel miękka). Kody połączenia te same (HOST_5G, WIRED…), więc zakładka Sieć jest wspólna.
"""
import re
from urllib.parse import urlsplit

import httpx

from . import ActionResult, ActionSpec, Adapter, AdapterError, Field, Found, PollResult, State, Update

NULL_SESSION = "0" * 32
SESSION_TIMEOUT = 120               # sesja rpcd wygasa sama — nie trzeba się wylogowywać
BACKUP_MAX = 20 * 1024 * 1024
PHONE = re.compile(r"iphone|ipad|android|galaxy|pixel|redmi|xiaomi|huawei|oneplus|phone|telefon", re.I)
PRESENT = {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"}
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
OPKG = ("/bin/opkg", ["list-upgradable"])
APK = ("/usr/bin/apk", ["list", "--upgradable"])
APK_VERSION = re.compile(r"^(.+)-(\d[^-\s]*(?:-r\d+)?)$")
APK_LINE = re.compile(r"^(\S+)\s.*\[upgradable from: (\S+)\]")
UBUS_ERRORS = {1: "nieprawidłowe polecenie", 2: "zły argument", 3: "nie ma takiej metody", 4: "nie znaleziono",
               5: "brak danych", 7: "przekroczony czas", 8: "nieobsługiwane"}


# Wklejany w SSH routera: dwa konta rpcd (odczyt i sama kopia), ACL z dokładnie tym, co czyta adapter, i cron
# odświeżający listy pakietów. Konta mają własne hasła (skrót w /etc/config/rpcd), nie są kontami systemu.
SETUP_SCRIPT = r"""HASLO_ODCZYT='wpisz-haslo-do-odczytu'
HASLO_KOPIA='wpisz-inne-haslo-do-kopii'

cat > /usr/share/rpcd/acl.d/denali.json <<'EOF'
{
  "denali": {
    "description": "Denali: tylko odczyt stanu",
    "read": {
      "ubus": {
        "system": ["board", "info"],
        "network.interface": ["dump"],
        "network.device": ["status"],
        "iwinfo": ["devices", "info", "assoclist"],
        "file": ["read", "exec"]
      },
      "file": {
        "/tmp/dhcp.leases": ["read"],
        "/sbin/ip -4 neigh show": ["exec"],
        "/bin/opkg list-upgradable": ["exec"],
        "/usr/bin/apk list --upgradable": ["exec"]
      }
    }
  },
  "denali-kopia": {
    "description": "Denali: tylko kopia konfiguracji",
    "read": { "cgi-io": ["backup"] }
  }
}
EOF
uci -q delete rpcd.denali; uci -q delete rpcd.denali_kopia
uci set rpcd.denali=login
uci set rpcd.denali.username='denali'
uci set rpcd.denali.password="$(uhttpd -m "$HASLO_ODCZYT")"
uci add_list rpcd.denali.read='denali'
uci set rpcd.denali_kopia=login
uci set rpcd.denali_kopia.username='denali-kopia'
uci set rpcd.denali_kopia.password="$(uhttpd -m "$HASLO_KOPIA")"
uci add_list rpcd.denali_kopia.read='denali-kopia'
uci commit rpcd
/etc/init.d/rpcd restart
grep -q '# denali' /etc/crontabs/root 2>/dev/null || \
  echo '17 4 * * * (command -v apk >/dev/null && apk update || opkg update) >/dev/null 2>&1 # denali' >> /etc/crontabs/root
/etc/init.d/cron enable; /etc/init.d/cron restart
(command -v apk >/dev/null && apk update || opkg update) >/dev/null 2>&1 &
echo "Gotowe: konta denali i denali-kopia"
"""


class _Denied(AdapterError):
    """Konto nie ma prawa do tego obiektu ubus (ACL) albo sesja wygasła."""


class _Missing(AdapterError):
    """Na routerze nie ma tego obiektu albo pliku (np. brak Wi-Fi, opkg na OpenWrt z apk)."""


def _mac(v):
    return str(v or "").strip().lower().replace("-", ":")


def band(freq):
    """Częstotliwość (MHz) → kod jak w TP-Linku (HOST_2G…)."""
    try:
        f = int(freq)
    except (TypeError, ValueError):
        return "HOST_2G"
    return "HOST_2G" if f < 3000 else "HOST_5G" if f < 5925 else "HOST_6G"


def speed(value):
    """„1000F” → „1 Gb/s”, „100H” → „100 Mb/s, półdupleks”; brak linku → ""."""
    m = re.fullmatch(r"(\d+)([FH])?", str(value or ""))
    if not m:
        return ""
    n = int(m.group(1))
    text = f"{n // 1000} Gb/s" if n >= 1000 and n % 1000 == 0 else f"{n / 1000:g} Gb/s" if n >= 1000 else f"{n} Mb/s"
    return text + (", półdupleks" if m.group(2) == "H" else "")


def leases(text):
    """/tmp/dhcp.leases → {mac: (ip, nazwa)}."""
    out = {}
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and MAC.match(_mac(parts[1])):
            out[_mac(parts[1])] = (parts[2], "" if parts[3] == "*" else parts[3])
    return out


def neighbours(text, skip_devs=()):
    """`ip -4 neigh show` → {mac: ip} — tylko wpisy z adresem MAC, aktualne albo niedawne, nie od strony WAN."""
    out = {}
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or "lladdr" not in parts or parts[-1] not in PRESENT:
            continue
        dev = parts[parts.index("dev") + 1] if "dev" in parts[:-1] else ""
        mac = _mac(parts[parts.index("lladdr") + 1])
        if dev in skip_devs or not MAC.match(mac):
            continue
        out[mac] = parts[0]
    return out


def _apk_split(full):
    m = APK_VERSION.match(full)
    return (m.group(1), m.group(2)) if m else (full, "")


def upgradable(stdout, apk=False):
    """Wyjście opkg / apk → [(pakiet, obecna, dostępna)]."""
    out = []
    for line in str(stdout or "").splitlines():
        line = line.strip()
        if apk:
            m = APK_LINE.match(line)
            if m:
                name, new = _apk_split(m.group(1))
                old_name, old = _apk_split(m.group(2))
                out.append((name if name == old_name else m.group(1), old, new))
        else:
            parts = [p.strip() for p in line.split(" - ")]
            if len(parts) == 3 and parts[0] and " " not in parts[0]:
                out.append(tuple(parts))
    return out


class Openwrt(Adapter):
    type = "openwrt"
    label = "Router OpenWrt"
    default_poll = 120
    fields = [
        Field("url", "Adres routera", "url", help="np. https://192.168.1.1 (tak jak otwierasz LuCI)"),
        Field("username", "Login", required=False, default="denali", help="konto rpcd ze skryptu obok (domyślnie denali)"),
        Field("password", "Hasło konta denali", "secret"),
        Field("as_ap", "To urządzenie działa jako punkt dostępowy (AP), nie główny router", "bool", required=False,
              default=False),
        Field("verify_tls", "Sprawdzaj certyfikat (OpenWrt ma domyślnie własny, niepodpisany)", "bool",
              required=False, default=False),
    ]
    actions = [
        ActionSpec("backup", "Kopia konfiguracji",
                   "Router przygotuje archiwum ustawień (to samo co sysupgrade -b w LuCI). Denali zapisze je "
                   "zaszyfrowane na karcie urządzenia. Na routerze nic się nie zmienia.", ("router",)),
    ]
    action_fields = [
        Field("backup_username", "Login konta do kopii", help="ze skryptu: denali-kopia"),
        Field("backup_password", "Hasło konta do kopii", "secret"),
    ]
    action_help = "Osobne konto rpcd z prawem tylko do kopii konfiguracji (cgi-io backup) — skrypt obok."
    setup_script = SETUP_SCRIPT

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip())
        if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/") or p.query:
            raise AdapterError("Adres: https://IP-routera (bez ścieżki).")

    # ───────────────────────── ubus ─────────────────────────
    @property
    def _base(self):
        return str(self.config["url"]).strip().rstrip("/")

    def _client(self):
        kw = {"transport": self.transport} if self.transport else {}
        return httpx.AsyncClient(timeout=15, follow_redirects=False,
                                 verify=bool(self.config.get("verify_tls", False)), **kw)

    async def _post(self, cli, path, **kw):
        try:
            return await cli.post(self._base + path, **kw)
        except httpx.HTTPError as e:
            raise AdapterError(f"Brak połączenia z {self._base} ({e.__class__.__name__}).") from None

    async def _call(self, cli, sid, obj, method, args=None):
        r = await self._post(cli, "/ubus", json={"jsonrpc": "2.0", "id": 1, "method": "call",
                                                  "params": [sid, obj, method, args or {}]})
        if r.status_code == 404:
            raise AdapterError("Router nie ma /ubus — zainstaluj LuCI albo uhttpd-mod-ubus.")
        try:
            body = r.json()
        except ValueError:
            raise AdapterError(f"Odpowiedź nie wygląda na OpenWrt (HTTP {r.status_code}).") from None
        where = f"{obj} {method}"
        err = body.get("error") if isinstance(body, dict) else None
        if err:
            code = err.get("code")
            if code == -32002:
                raise _Denied(f"Konto nie ma dostępu do „{where}” — sprawdź ACL ze skryptu.")
            if code in (-32000, -32601):
                raise _Missing(f"Na routerze nie ma „{where}”.")
            raise AdapterError(f"Router odrzucił „{where}”: {err.get('message') or code}")
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list) or not result:
            raise AdapterError("Odpowiedź nie wygląda na OpenWrt.")
        status = result[0]
        if status == 6:
            raise _Denied(f"Konto nie ma dostępu do „{where}” — sprawdź ACL ze skryptu.")
        if status in (3, 4):
            raise _Missing(f"Na routerze nie ma „{where}”.")
        if status != 0:
            raise AdapterError(f"Router: „{where}” — {UBUS_ERRORS.get(status, f'błąd {status}')}.")
        return result[1] if len(result) > 1 and isinstance(result[1], dict) else {}

    async def _optional(self, cli, sid, obj, method, args=None):
        try:
            return await self._call(cli, sid, obj, method, args)
        except (_Denied, _Missing):
            return None

    async def _login(self, cli, username, password):
        try:
            res = await self._call(cli, NULL_SESSION, "session", "login",
                                   {"username": username, "password": password, "timeout": SESSION_TIMEOUT})
        except _Denied:
            raise AdapterError(f"Router odrzucił login „{username}” albo hasło.") from None
        sid = res.get("ubus_rpc_session")
        if not sid:
            raise AdapterError("Router nie dał sesji — to na pewno OpenWrt?")
        return sid

    async def _exec(self, cli, sid, command, params):
        res = await self._call(cli, sid, "file", "exec", {"command": command, "params": params})
        return str(res.get("stdout") or "")

    async def _read_file(self, cli, sid, path):
        return str((await self._optional(cli, sid, "file", "read", {"path": path}) or {}).get("data") or "")

    # ───────────────────────── odczyt ─────────────────────────
    async def _updates(self, cli, sid):
        """[(pakiet, obecna, dostępna)] albo None (konto nie może sprawdzić)."""
        for (cmd, params), apk in ((OPKG, False), (APK, True)):
            try:
                return upgradable(await self._exec(cli, sid, cmd, params), apk)
            except (_Denied, _Missing):
                continue
        return None

    async def _wifi(self, cli, sid):
        """[(sieć, [klienci])] — tylko interfejsy w trybie AP."""
        devs = (await self._optional(cli, sid, "iwinfo", "devices") or {}).get("devices") or []
        out = []
        for dev in devs:
            info = await self._optional(cli, sid, "iwinfo", "info", {"device": dev}) or {}
            if str(info.get("mode") or "").lower() not in ("master", "ap"):
                continue
            assoc = (await self._optional(cli, sid, "iwinfo", "assoclist", {"device": dev}) or {}).get("results") or []
            out.append(({"device": dev, "ssid": info.get("ssid") or "", "band": band(info.get("frequency")),
                         "channel": info.get("channel")}, assoc))
        return out

    async def _collect(self):
        username = str(self.config.get("username") or "denali").strip()
        async with self._client() as cli:
            sid = await self._login(cli, username, self.secrets["password"])
            board = await self._call(cli, sid, "system", "board")
            info = await self._optional(cli, sid, "system", "info") or {}
            ifaces = (await self._optional(cli, sid, "network.interface", "dump") or {}).get("interface") or []
            devices = await self._optional(cli, sid, "network.device", "status") or {}
            wan_devs = {i.get("l3_device") or i.get("device") for i in ifaces
                        if str(i.get("interface") or "").startswith("wan")} - {None}
            try:
                neigh = neighbours(await self._exec(cli, sid, "/sbin/ip", ["-4", "neigh", "show"]), wan_devs)
            except (_Denied, _Missing):
                neigh = None
            return {"board": board, "info": info, "ifaces": ifaces, "devices": devices, "neigh": neigh,
                    "leases": leases(await self._read_file(cli, sid, "/tmp/dhcp.leases")),
                    "wifi": await self._wifi(cli, sid), "updates": await self._updates(cli, sid)}

    @staticmethod
    def _model(board):
        release = board.get("release") or {}
        return (str(board.get("model") or "OpenWrt").strip(),
                str(release.get("description") or f"OpenWrt {release.get('version') or ''}").strip())

    async def test(self):
        d = await self._collect()
        model, firmware = self._model(d["board"])
        missing = [what for what, ok in (("klienci", d["neigh"] is not None or d["leases"]),
                                         ("aktualizacje", d["updates"] is not None)) if not ok]
        return (f"Połączono: {model} · {firmware}"
                + (f" · bez uprawnień: {', '.join(missing)} (ACL ze skryptu)" if missing else ""))

    def _ports(self, devices):
        """Porty fizyczne: członkowie mostów (bez Wi-Fi) i urządzenie WAN."""
        names = []
        for name, d in devices.items():
            if isinstance(d, dict) and d.get("type") == "bridge":
                names += [m for m in d.get("bridge-members") or [] if not str(m).startswith(("phy", "wlan"))]
        names += [n for n in ("wan", "eth1") if n in devices and n not in names][:1]
        out = []
        for n in names:
            d = devices.get(n) or {}
            up = bool(d.get("carrier", d.get("up")))
            out.append({"name": n, "up": up, "speed": speed(d.get("speed")) if up else ""})
        return out

    @staticmethod
    def _wan(ifaces):
        wan = next((i for i in ifaces if i.get("interface") == "wan"), None)
        if not wan:
            return None
        addrs = wan.get("ipv4-address") or []
        return {"up": bool(wan.get("up")), "proto": wan.get("proto") or "", "uptime": wan.get("uptime"),
                "ip": addrs[0].get("address") if addrs else "", "device": wan.get("l3_device") or wan.get("device")}

    async def poll(self):
        d = await self._collect()
        out = PollResult()
        board, info = d["board"], d["info"]
        model, firmware = self._model(board)
        lan = next((i for i in d["ifaces"] if i.get("interface") == "lan"), {})
        lan_ip = ((lan.get("ipv4-address") or [{}])[0]).get("address") or ""
        lan_dev = d["devices"].get(lan.get("l3_device") or lan.get("device") or "br-lan") or {}
        own = {_mac(v.get("macaddr")) for v in d["devices"].values() if isinstance(v, dict) and v.get("macaddr")}
        lan_mac = _mac(lan_dev.get("macaddr"))
        addrs = ([{"kind": "ip", "value": lan_ip}] if lan_ip else []) + ([{"kind": "mac", "value": lan_mac}] if lan_mac else [])
        as_ap = bool(self.config.get("as_ap"))
        host = str(board.get("hostname") or "").strip()
        out.found.append(Found("router", {"name": host if host and host != "OpenWrt" else model,
                                          "kind": "ap" if as_ap else "router", "addresses": addrs}))

        # klienci: Wi-Fi (na pewno są) + sąsiedzi z tablicy ARP (kabel albo za AP); nazwa i IP z DHCP
        clients = {}
        for net, assoc in d["wifi"]:
            for a in assoc:
                mac = _mac(a.get("mac"))
                if MAC.match(mac):
                    clients[mac] = {"connection": net["band"], "signal": a.get("signal"), "ssid": net["ssid"]}
        for mac in (d["neigh"] if d["neigh"] is not None else d["leases"]):
            clients.setdefault(mac, {"connection": "WIRED", "signal": None, "ssid": ""})
        for mac, c in clients.items():
            if mac in own:
                continue
            lease_ip, name = d["leases"].get(mac, ("", ""))
            ip = (d["neigh"] or {}).get(mac) or lease_ip
            wired = c["connection"] == "WIRED"
            out.found.append(Found(f"mac/{mac}", {
                "name": name or mac, "kind": "telefon" if PHONE.search(name) else "komputer",
                "addresses": [{"kind": "mac", "value": mac}] + ([{"kind": "ip", "value": ip}] if ip else []),
                "uplink": {"ref": "router", "hard": not wired}}))
            how = "kabel" if wired else {"HOST_2G": "Wi-Fi 2,4 GHz", "HOST_5G": "Wi-Fi 5 GHz",
                                         "HOST_6G": "Wi-Fi 6 GHz"}[c["connection"]]
            summary = (f"w sieci · {how}" + (f" „{c['ssid']}”" if c["ssid"] else "")
                       + (f" · sygnał {c['signal']} dBm" if c["signal"] else "") + (f" · {ip}" if ip else ""))
            out.states.append(State(f"mac/{mac}", "ok", summary, {"connection": c["connection"], "signal": c["signal"],
                                                                  "ssid": c["ssid"]}))

        wan = None if as_ap else self._wan(d["ifaces"])
        ports = self._ports(d["devices"])
        wifi = [{**net, "clients": len(assoc)} for net, assoc in d["wifi"]]
        n_wifi = sum(1 for c in clients.values() if c["connection"] != "WIRED")
        n_all = len([m for m in clients if m not in own])
        parts = [f"{model} · {firmware}", f"klienci: {n_all} (Wi-Fi {n_wifi}, kabel {n_all - n_wifi})"]
        state = "ok"
        if wan is not None:
            if wan["up"]:
                parts.append(f"WAN {wan['ip'] or wan['proto']}")
            else:
                state, parts = "warn", parts + ["WAN nie działa"]
        mem = info.get("memory") or {}
        mem_used = round(100 - 100 * (mem.get("available") or mem.get("free") or 0) / mem["total"]) if mem.get("total") else None
        updates = d["updates"]
        if updates:
            parts.append(f"aktualizacje: {len(updates)}")
        out.states.append(State("router", state, " · ".join(parts), {
            "kind": "openwrt", "model": model, "firmware": firmware, "hostname": host,
            "target": (board.get("release") or {}).get("target") or "", "uptime": info.get("uptime"),
            "load": round((info.get("load") or [0])[0] / 65536, 2) if info.get("load") else None,
            "memory_used": mem_used, "wan": wan, "ports": ports, "wifi": wifi, "clients": n_all,
            "updates": len(updates) if updates is not None else None}))
        if updates is not None:
            out.updates = [Update("router", name, "pakiet", cur, new) for name, cur, new in updates]
        return out

    # ───────────────────────── akcja: kopia konfiguracji ─────────────────────────
    async def run_action(self, key, external_id, data, params):
        if key != "backup" or external_id != "router":
            raise AdapterError("Nieznana akcja.")
        user = str(self.action_secrets.get("backup_username") or "").strip()
        password = str(self.action_secrets.get("backup_password") or "")
        if not user or not password:
            raise AdapterError("Brak konta do kopii konfiguracji.")
        async with self._client() as cli:
            sid = await self._login(cli, user, password)
            r = await self._post(cli, "/cgi-bin/cgi-backup", data={"sessionid": sid})
        if r.status_code == 403:
            raise AdapterError(f"Konto „{user}” nie ma prawa do kopii (ACL: cgi-io backup).")
        if r.status_code == 404:
            raise AdapterError("Router nie ma /cgi-bin/cgi-backup (pakiet cgi-io, OpenWrt 19.07+).")
        body = r.content
        if r.status_code != 200 or not body.startswith(b"\x1f\x8b"):
            raise AdapterError(f"Router nie oddał archiwum (HTTP {r.status_code}).")
        if len(body) > BACKUP_MAX:
            raise AdapterError("Archiwum jest podejrzanie duże — nie zapisano.")
        host = re.sub(r"[^A-Za-z0-9._-]", "-", str((data or {}).get("hostname") or "openwrt")) or "openwrt"
        return ActionResult(True, f"Kopia konfiguracji: {max(1, len(body) // 1024)} KB",
                            file={"name": f"backup-{host}.tar.gz", "data": body})
