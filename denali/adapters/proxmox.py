"""Proxmox VE: węzły, VM i kontenery LXC — stan, zasoby, adresy IP. Odczyt tokenem z rolą PVEAuditor.

Akcje (start, wyłączenie, restart, zatrzymanie, snapshot VM i LXC) — osobnym tokenem z rolą PanelActions
(VM.PowerMgmt, VM.Snapshot). Bez tego tokenu panel nie ma czym ich wykonać. Proxmox zwraca zadanie (UPID),
którego koniec sprawdzamy osobno.

Zatrzymana maszyna to „off” (wyłączona celowo), chyba że ma startować z hostem (onboot) — wtedy „down”.
Aktualizacje węzła: lista pakietów z `/nodes/<węzeł>/apt/update` (ta sama co w Proxmoxie: Węzeł → Updates;
Proxmox odświeża ją sam raz dziennie). PVEAuditor ją widzi (Sys.Audit).
"""
import asyncio
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from . import Adapter, ActionResult, ActionSpec, AdapterError, Field, Found, PollResult, State, apt_updates, lan_iface
from .tls import clean_pem as _clean_pem, ssl_context, validate_pem_field

GB = 1024 ** 3
CA_HINT = "cat /etc/pve/pve-root-ca.pem"
GUESTS = ("qemu/", "lxc/")
RUNNING = ("ok", "warn")
SNAPNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")


def clean_pem(text: str) -> str:
    return _clean_pem(text, CA_HINT)


def _ssl_context(ca_pem: str, verify: bool):
    return ssl_context(ca_pem, verify, CA_HINT)


def _days(seconds):
    d = int(seconds or 0) // 86400
    return f"{d} {'dzień' if d == 1 else 'dni'}" if d else "od niedawna"


def _ram(mem, maxmem):
    if not maxmem:
        return ""
    return f"RAM {mem / GB:.1f}/{maxmem / GB:.0f} GB".replace(".", ",")


class Proxmox(Adapter):
    type = "proxmox"
    removable = ("qemu/", "lxc/", "node/")
    label = "Proxmox VE"
    default_poll = 60
    fields = [
        Field("url", "Adres", "url", help="np. https://192.168.1.2:8006 — bez ścieżki"),
        Field("token_id", "Identyfikator tokenu", help="użytkownik@realm!nazwa, np. denali@pve!odczyt"),
        Field("token_secret", "Sekret tokenu", "secret"),
        Field("ca_pem", "Certyfikat CA Proxmoxa (pve-root-ca.pem)", "textarea", required=False,
              help="Zawartość /etc/pve/pve-root-ca.pem. Bez tego nie da się sprawdzić certyfikatu z własnym podpisem."),
        Field("verify_tls", "Sprawdzaj certyfikat", "bool", required=False, default=True,
              help="Wyłącz tylko, jeśli nie możesz wkleić certyfikatu CA (połączenie dalej szyfrowane, ale bez pewności, z kim)."),
    ]

    actions = [
        ActionSpec("start", "Uruchom", "Proxmox uruchomi maszynę.", GUESTS, when=("off", "down", "unknown")),
        ActionSpec("shutdown", "Wyłącz", "Proxmox poprosi system w maszynie o wyłączenie (ACPI albo agent QEMU). "
                   "Usługi na niej przestaną działać, dopóki ktoś jej nie uruchomi.", GUESTS, True, RUNNING),
        ActionSpec("reboot", "Uruchom ponownie", "System w maszynie zostanie czysto zrestartowany. Przez chwilę "
                   "usługi na niej nie będą działać.", GUESTS, True, RUNNING),
        ActionSpec("stop", "Zatrzymaj natychmiast", "Jak wyjęcie wtyczki: maszyna zatrzyma się od razu, bez "
                   "zamykania systemu. Niezapisane dane przepadną. Tylko gdy „Wyłącz” nie działa.", GUESTS, True,
                   RUNNING + ("unknown",)),
        ActionSpec("snapshot", "Zrób snapshot", "Proxmox zapisze snapshot maszyny (dyski, bez pamięci RAM). "
                   "Wrócić do niego można w Proxmoxie (maszyna → Snapshots → Rollback).", GUESTS),
    ]
    action_fields = [
        Field("action_token_id", "Identyfikator tokenu do akcji", help="np. denali@pve!akcje"),
        Field("action_token_secret", "Sekret tokenu do akcji", "secret"),
    ]

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip().rstrip("/"))
        if p.scheme != "https" or not p.hostname or p.path or p.query or p.username:
            raise AdapterError("Adres: https://IP-albo-nazwa:8006, bez ścieżki.")
        tid = str(config.get("token_id", "")).strip()
        if "@" not in tid or "!" not in tid:
            raise AdapterError("Identyfikator tokenu ma postać użytkownik@realm!nazwa, np. denali@pve!odczyt.")
        validate_pem_field(config, CA_HINT)          # zapisujemy oczyszczony; zły certyfikat → błąd na formularzu

    @classmethod
    def validate_action_secrets(cls, values):
        super().validate_action_secrets(values)
        tid = str(values.get("action_token_id", "")).strip()
        if "@" not in tid or "!" not in tid:
            raise AdapterError("Identyfikator tokenu ma postać użytkownik@realm!nazwa, np. denali@pve!akcje.")

    def _client(self, action=False):
        url = str(self.config["url"]).strip().rstrip("/")
        if action:
            token = f"{self.action_secrets['action_token_id'].strip()}={self.action_secrets['action_token_secret'].strip()}"
        else:
            token = f"{self.config['token_id'].strip()}={self.secrets['token_secret'].strip()}"
        headers = {"Authorization": f"PVEAPIToken={token}", "Accept": "application/json"}
        kw = {"transport": self.transport} if self.transport else {
            "verify": _ssl_context(self.config.get("ca_pem", ""), bool(self.config.get("verify_tls", True)))}
        return httpx.AsyncClient(base_url=url + "/api2/json", headers=headers, timeout=10, follow_redirects=False, **kw)

    async def _get(self, cli, path, timeout=None):
        try:
            r = await cli.get(path, timeout=timeout or 10)
        except httpx.ConnectError as e:
            if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
                raise AdapterError("Certyfikat Proxmoxa nie jest zaufany — wklej pve-root-ca.pem albo wyłącz "
                                   "sprawdzanie certyfikatu.") from None
            raise AdapterError(f"Brak połączenia z {self.config['url']} — sprawdź adres i czy Proxmox działa.") from None
        except httpx.TimeoutException:
            raise AdapterError(f"{self.config['url']} nie odpowiada (przekroczony czas).") from None
        except httpx.HTTPError as e:
            raise AdapterError(f"Błąd połączenia: {e.__class__.__name__}") from None
        if r.status_code == 401:
            raise AdapterError("Proxmox odrzucił token — sprawdź identyfikator i sekret.")
        if r.status_code == 403:
            raise AdapterError(f"Token nie ma uprawnień do {path} — nadaj mu rolę PVEAuditor na „/”.")
        if r.status_code >= 400:
            raise AdapterError(f"Proxmox zwrócił błąd {r.status_code} dla {path}.")
        try:
            return r.json().get("data")
        except ValueError:
            raise AdapterError("Odpowiedź nie wygląda na API Proxmoxa — sprawdź adres i port (zwykle 8006).") from None

    async def test(self):
        async with self._client() as cli:
            ver = await self._get(cli, "/version") or {}
            res = await self._get(cli, "/cluster/resources") or []
        nodes = sum(1 for r in res if r.get("type") == "node")
        guests = sum(1 for r in res if r.get("type") in ("qemu", "lxc") and not r.get("template"))
        if not nodes:
            raise AdapterError("Połączono, ale token nie widzi żadnego węzła — nadaj mu rolę PVEAuditor na „/” "
                               "(przy tokenie z oddzielonymi uprawnieniami: także samemu tokenowi).")
        if not any(r.get("maxmem") for r in res if r.get("type") == "node"):
            raise AdapterError(f"Połączono, ale token widzi tylko nazwy węzłów (bez CPU, RAM i maszyn: {guests}) — "
                               "dodaj tokenowi rolę PVEAuditor na „/” (Datacenter → Permissions → API Token Permission).")
        return f"Połączono: Proxmox VE {ver.get('version', '?')} · węzły: {nodes} · maszyny i kontenery: {guests}"

    async def _guest_ips(self, cli, g):
        node, vmid, typ = g["node"], g["vmid"], g["type"]
        ips = []
        try:
            if typ == "qemu":
                data = await self._get(cli, f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces", timeout=5) or {}
                for iface in data.get("result", []):
                    if not lan_iface(iface.get("name", "")):
                        continue
                    ips += [a["ip-address"] for a in iface.get("ip-addresses", [])
                            if a.get("ip-address-type") == "ipv4" and not a.get("ip-address", "").startswith("127.")]
            else:
                for iface in await self._get(cli, f"/nodes/{node}/lxc/{vmid}/interfaces", timeout=5) or []:
                    ip = (iface.get("inet") or "").split("/")[0]
                    if lan_iface(iface.get("name")) and ip and not ip.startswith("127."):
                        ips.append(ip)
        except AdapterError:
            return []           # brak agenta QEMU albo uprawnień — maszyna i tak trafia do panelu, bez IP
        return ips

    async def _onboot(self, cli, g):
        try:
            cfg = await self._get(cli, f"/nodes/{g['node']}/{g['type']}/{g['vmid']}/config", timeout=5) or {}
        except AdapterError:
            return False
        return str(cfg.get("onboot", "0")) == "1"

    async def poll(self):
        async with self._client() as cli:
            res = await self._get(cli, "/cluster/resources") or []
            try:
                status = await self._get(cli, "/cluster/status") or []
            except AdapterError:
                status = []
            node_ip = {s.get("name"): s.get("ip") for s in status if s.get("type") == "node" and s.get("ip")}
            nodes = [r for r in res if r.get("type") == "node" and r.get("node")]
            guests = [r for r in res if r.get("type") in ("qemu", "lxc") and str(r.get("vmid", "")).isdigit()
                      and not r.get("template")]
            sem = asyncio.Semaphore(5)

            async def details(g):
                async with sem:
                    if g.get("status") == "running":
                        return await self._guest_ips(cli, g), None
                    return [], await self._onboot(cli, g)

            extra = await asyncio.gather(*(details(g) for g in guests))
            node_updates = {}
            for n in nodes:
                if n.get("status") != "online":
                    continue
                try:
                    node_updates[n["node"]] = await self._get(cli, f"/nodes/{n['node']}/apt/update") or []
                except AdapterError:
                    pass        # brak uprawnień albo węzeł nie odpowiada — zostaje poprzednia lista

        out = PollResult()
        if node_updates:
            out.updates = [u for name, pkgs in node_updates.items() for u in apt_updates(f"node/{name}", pkgs)]
        for n in nodes:
            name = n["node"]
            eid = f"node/{name}"
            addrs = [{"kind": "ip", "value": node_ip[name]}] if node_ip.get(name) else []
            out.found.append(Found(eid, {"name": name, "kind": "host", "addresses": addrs}))
            online = n.get("status") == "online"
            out.states.append(State(eid, "ok" if online else "down",
                                    f"węzeł działa · {_days(n.get('uptime'))}" if online else "węzeł niedostępny",
                                    {"cpu": n.get("cpu"), "mem": n.get("mem"), "maxmem": n.get("maxmem"),
                                     "uptime": n.get("uptime")}))
        for g, (ips, onboot) in zip(guests, extra):
            typ, vmid, node = g["type"], g["vmid"], g["node"]
            eid = f"{typ}/{vmid}"
            label = "VM" if typ == "qemu" else "LXC"
            out.found.append(Found(eid, {
                "name": g.get("name") or eid, "kind": "vm" if typ == "qemu" else "lxc",
                "parent": {"ref": f"node/{node}"}, "addresses": [{"kind": "ip", "value": ip} for ip in ips]}))
            st = g.get("status", "unknown")
            if st == "running":
                parts = [f"{label} {vmid} działa · {_days(g.get('uptime'))}", _ram(g.get("mem") or 0, g.get("maxmem"))]
                state, summary = "ok", " · ".join(p for p in parts if p)
            elif st == "paused":
                state, summary = "warn", f"{label} {vmid} wstrzymana"
            elif st == "stopped":
                state, summary = ("down", f"{label} {vmid} zatrzymana, a ma startować z hostem") if onboot \
                    else ("off", f"{label} {vmid} wyłączona")
            else:
                state, summary = "unknown", f"{label} {vmid}: {st}"
            out.states.append(State(eid, state, summary, {
                "node": node, "vmid": vmid, "type": typ, "status": st, "cpu": g.get("cpu"), "mem": g.get("mem"),
                "maxmem": g.get("maxmem"), "disk": g.get("disk"), "maxdisk": g.get("maxdisk"),
                "uptime": g.get("uptime"), "onboot": onboot}))
        return out

    # ───────────────────────── akcje ─────────────────────────
    async def _call(self, cli, method, path, data=None):
        try:
            r = await cli.request(method, path, data=data, timeout=20)
        except httpx.TimeoutException:
            raise AdapterError(f"{self.config['url']} nie odpowiada (przekroczony czas).") from None
        except httpx.HTTPError:
            raise AdapterError(f"Brak połączenia z {self.config['url']}.") from None
        if r.status_code == 401:
            raise AdapterError("Proxmox odrzucił token do akcji — sprawdź identyfikator i sekret.")
        if r.status_code == 403:
            raise AdapterError("Token do akcji nie ma uprawnień — nadaj mu rolę PanelActions (VM.PowerMgmt, "
                               "VM.Snapshot) na „/vms”.")
        if r.status_code >= 400:
            # Proxmox podaje powód w linii statusu, np. „500 VM 107 not running”
            raise AdapterError(f"Proxmox: {r.reason_phrase or r.status_code}"[:300])
        try:
            return r.json().get("data")
        except ValueError:
            raise AdapterError("Odpowiedź nie wygląda na API Proxmoxa.") from None

    async def run_action(self, key, external_id, data, params):
        typ, _, vmid = external_id.partition("/")
        node = (data or {}).get("node")
        if typ not in ("qemu", "lxc") or not vmid.isdigit() or not node:
            raise AdapterError("Nie wiadomo, na którym węźle jest ta maszyna — poczekaj na odczyt Proxmoxa.")
        base = f"/nodes/{quote(str(node), safe='')}/{typ}/{vmid}"
        async with self._client(action=True) as cli:
            if key == "snapshot":
                name = params.get("snapname") or time.strftime("denali-%Y%m%d-%H%M%S")
                if not SNAPNAME.match(name):
                    raise AdapterError("Zła nazwa snapshotu.")
                upid = await self._call(cli, "POST", base + "/snapshot", {
                    "snapname": name, "description": str(params.get("description") or "Denali")[:200]})
                done = f"snapshot {name}"
            elif key in ("start", "shutdown", "reboot", "stop"):
                upid = await self._call(cli, "POST", f"{base}/status/{key}")
                done = "zlecone w Proxmoxie"
            else:
                raise AdapterError("Nieznana akcja.")
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return ActionResult(True, done)
        return ActionResult(True, done, task={"node": node, "upid": upid, "text": done})

    async def check_task(self, task):
        async with self._client(action=True) as cli:
            st = await self._call(cli, "GET", f"/nodes/{quote(str(task['node']), safe='')}/tasks/"
                                              f"{quote(task['upid'], safe='')}/status") or {}
        if st.get("status") != "stopped":
            return None
        exit_ = str(st.get("exitstatus") or "")
        if exit_ == "OK" or exit_.startswith("WARNINGS"):
            return ActionResult(True, (task.get("text") or "zakończone") + (f" ({exit_})" if exit_ != "OK" else ""))
        return ActionResult(False, f"Proxmox: {exit_ or 'zadanie nie powiodło się'}"[:300])
