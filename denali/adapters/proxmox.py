"""Proxmox VE: węzły, VM i kontenery LXC — stan, zasoby, adresy IP.

Jeden token do wszystkiego. Odczyt to wbudowana rola PVEAuditor (jej skład zależy od wersji Proxmoxa, więc nie
wypisujemy go sami). Administrator wybiera zakresy (Grant): akcje i konsolę, a panel układa polecenie pveum z dokładnie
tymi uprawnieniami i po zapisie sprawdza w /access/permissions, czy token je ma.

Akcje (start, wyłączenie, restart, zatrzymanie, snapshot VM i LXC): Proxmox zwraca zadanie (UPID), którego koniec
sprawdzamy osobno. Konsola VM i LXC (karta Terminale): vncproxy daje jednorazowy bilet, a panel przekazuje połączenie
noVNC przez vncwebsocket. Bez hasła do systemu w maszynie konsola kończy się na ekranie logowania.

Zatrzymana maszyna to „off” (wyłączona celowo), chyba że ma startować z hostem (onboot) — wtedy „down”.
Aktualizacje węzła: lista pakietów z `/nodes/<węzeł>/apt/update` (ta sama co w Proxmoxie: Węzeł → Updates;
Proxmox odświeża ją sam raz dziennie). PVEAuditor ją widzi (Sys.Audit).
"""
import asyncio
import itertools
import re
import shlex
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx
import websockets

from . import (Adapter, ActionResult, ActionSpec, AdapterError, Field, Found, Grant, PollResult, State, apt_updates,
               lan_iface)
from .tls import clean_pem as _clean_pem, ssl_context, validate_pem_field

GB = 1024 ** 3
CA_HINT = "cat /etc/pve/pve-root-ca.pem"
GUESTS = ("qemu/", "lxc/")
RUNNING = ("ok", "warn")
SNAPNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
ROLE = "DenaliPanel"
GUEST_ACL = "/vms"
READ_PRIV = "VM.Audit"
DEFAULT_TOKEN = "denali@pve!panel"


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
        Field("url", "Adres", "url", help="bezpośrednio do węzła, np. https://192.168.1.2:8006 — bez ścieżki i bez proxy"),
        Field("token_id", "Identyfikator tokenu", help="użytkownik@realm!nazwa", default=DEFAULT_TOKEN),
        Field("token_secret", "Sekret tokenu", "secret"),
        Field("ca_pem", "Certyfikat CA Proxmoxa (pve-root-ca.pem)", "textarea", required=False,
              help="Zawartość /etc/pve/pve-root-ca.pem. Bez tego nie da się sprawdzić certyfikatu z własnym podpisem."),
        Field("verify_tls", "Sprawdzaj certyfikat", "bool", required=False, default=True,
              help="Wyłącz tylko, jeśli nie możesz wkleić certyfikatu CA (połączenie dalej szyfrowane, ale bez pewności, z kim)."),
    ]

    grants = [
        Grant("akcje", "Akcje", "Start, wyłączenie, restart, zatrzymanie i snapshot maszyn.", ("VM.PowerMgmt", "VM.Snapshot")),
        Grant("konsola", "Konsola", "Ekran VM i LXC na karcie Terminale.", ("VM.Console",)),
    ]
    endpoint_fields = ("url", "ca_pem", "verify_tls")

    actions = [
        ActionSpec("start", "Uruchom", "Proxmox uruchomi maszynę.", GUESTS, when=("off", "down", "unknown"), grant="akcje"),
        ActionSpec("shutdown", "Wyłącz", "Proxmox poprosi system w maszynie o wyłączenie (ACPI albo agent QEMU). "
                   "Usługi na niej przestaną działać, dopóki ktoś jej nie uruchomi.", GUESTS, True, RUNNING, grant="akcje"),
        ActionSpec("reboot", "Uruchom ponownie", "System w maszynie zostanie czysto zrestartowany. Przez chwilę "
                   "usługi na niej nie będą działać.", GUESTS, True, RUNNING, grant="akcje"),
        ActionSpec("stop", "Zatrzymaj natychmiast", "Jak wyjęcie wtyczki: maszyna zatrzyma się od razu, bez "
                   "zamykania systemu. Niezapisane dane przepadną. Tylko gdy „Wyłącz” nie działa.", GUESTS, True,
                   RUNNING + ("unknown",), grant="akcje"),
        ActionSpec("snapshot", "Zrób snapshot", "Proxmox zapisze snapshot maszyny (dyski, bez pamięci RAM). "
                   "Wrócić do niego można w Proxmoxie (maszyna → Snapshots → Rollback).", GUESTS, grant="akcje"),
        ActionSpec("console", "Konsola (VNC)", "Ekran maszyny w przeglądarce: kto ma konsolę, może się zalogować do "
                   "systemu albo przerwać jego start.", GUESTS, True, RUNNING, terminal=True, grant="konsola"),
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
    def setup_command(cls, selected, token_id=""):
        """Polecenie do powłoki węzła: nadaje dokładnie wybrane zakresy. token_id = token już zapisany w panelu."""
        tid = token_id or DEFAULT_TOKEN
        user, _, name = tid.partition("!")
        who = f"--users {shlex.quote(user)}" + (f" --tokens {shlex.quote(tid)}" if token_id else "")
        privs = ",".join(p for g in cls.grants if g.key in selected for p in g.privs)
        lines = [f"pveum user add {shlex.quote(user)} --comment Denali 2>/dev/null",
                 f"pveum acl modify / {who} --roles PVEAuditor"]
        if privs:
            # wpis na głębszej ścieżce zastępuje odziedziczony z „/”, więc odczyt maszyn trzeba nadać na niej jeszcze raz
            lines += [f'pveum role add {ROLE} --privs "{privs}" 2>/dev/null || pveum role modify {ROLE} --privs "{privs}"',
                      f"pveum acl modify {GUEST_ACL} {who} --roles PVEAuditor",
                      f"pveum acl modify {GUEST_ACL} {who} --roles {ROLE}"]
        elif token_id:
            lines.append(f"pveum acl delete {GUEST_ACL} {who} --roles {ROLE} 2>/dev/null")
        if not token_id:
            lines += [f"pveum user token add {shlex.quote(user)} {shlex.quote(name)} --privsep 0",
                      "cat /etc/pve/pve-root-ca.pem"]
        return "\n".join(lines)

    @classmethod
    def setup_commands(cls, token_id=""):
        """Polecenie dla każdego układu zaznaczonych pól: {„akcje,konsola”: tekst}."""
        keys = [g.key for g in cls.grants]
        combos = itertools.chain.from_iterable(itertools.combinations(keys, n) for n in range(len(keys) + 1))
        return {",".join(c): cls.setup_command(c, token_id) for c in combos}

    def _client(self):
        url = str(self.config["url"]).strip().rstrip("/")
        headers = {"Authorization": f"PVEAPIToken={self._token()}", "Accept": "application/json"}
        kw = {"transport": self.transport} if self.transport else {
            "verify": _ssl_context(self.config.get("ca_pem", ""), bool(self.config.get("verify_tls", True)))}
        return httpx.AsyncClient(base_url=url + "/api2/json", headers=headers, timeout=10, follow_redirects=False, **kw)

    def _token(self):
        return f"{self.config['token_id'].strip()}={self.secrets['token_secret'].strip()}"

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
            raise AdapterError(f"Token nie ma uprawnień do {path} — uruchom polecenie z sekcji „Uprawnienia”.")
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
            raise AdapterError("Połączono, ale token nie widzi żadnego węzła — uruchom polecenie z sekcji „Uprawnienia”.")
        if not any(r.get("maxmem") for r in res if r.get("type") == "node"):
            raise AdapterError(f"Połączono, ale token widzi tylko nazwy węzłów (bez CPU, RAM i maszyn: {guests}) — "
                               "uruchom polecenie z sekcji „Uprawnienia”.")
        if missing := await self._missing_privs():
            raise AdapterError(f"Połączono, ale token nie ma uprawnień: {', '.join(missing)}. "
                               "Uruchom polecenie z sekcji „Uprawnienia”.")
        return f"Połączono: Proxmox VE {ver.get('version', '?')} · węzły: {nodes} · maszyny i kontenery: {guests}"

    async def _missing_privs(self):
        """Uprawnienia (odczyt maszyn i wybrane zakresy), których token nie ma. Gdy Proxmox tego nie poda — pusta lista."""
        wanted = [READ_PRIV] + [p for g in self.grants if g.key in (self.config.get("permissions") or []) for p in g.privs]
        try:
            async with self._client() as cli:
                have = await self._get(cli, f"/access/permissions?path={quote(GUEST_ACL, safe='')}")
        except AdapterError:
            return []
        if not isinstance(have, dict) or not isinstance(have.get(GUEST_ACL), dict):
            return []
        return [p for p in wanted if not have[GUEST_ACL].get(p)]

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
            raise AdapterError("Proxmox odrzucił token — sprawdź identyfikator i sekret.")
        if r.status_code == 403:
            raise AdapterError("Token nie ma tego uprawnienia w Proxmoxie — uruchom polecenie z sekcji „Uprawnienia” "
                               "w integracji.")
        if r.status_code >= 400:
            # Proxmox podaje powód w linii statusu, np. „500 VM 107 not running”
            raise AdapterError(f"Proxmox: {r.reason_phrase or r.status_code}"[:300])
        try:
            return r.json().get("data")
        except ValueError:
            raise AdapterError("Odpowiedź nie wygląda na API Proxmoxa.") from None

    @staticmethod
    def _guest_path(external_id, data):
        typ, _, vmid = external_id.partition("/")
        node = (data or {}).get("node")
        if typ not in ("qemu", "lxc") or not vmid.isdigit() or not node:
            raise AdapterError("Nie wiadomo, na którym węźle jest ta maszyna — poczekaj na odczyt Proxmoxa.")
        return f"/nodes/{quote(str(node), safe='')}/{typ}/{vmid}"

    async def console_ticket(self, external_id, data):
        """Bilet na konsolę: {path, port, ticket}. Proxmox czeka na połączenie około 10 sekund."""
        path = self._guest_path(external_id, data)
        async with self._client() as cli:
            res = await self._call(cli, "POST", path + "/vncproxy", {"websocket": 1})
        if not isinstance(res, dict) or not res.get("port") or not res.get("ticket"):
            raise AdapterError("Proxmox nie podał biletu na konsolę.")
        # przy websocket=1 bilet to „hasło:bilet”; hasło VNC Proxmox podaje też osobno
        return {"path": path, "port": res["port"], "ticket": res["ticket"], "password": res.get("password") or res["ticket"]}

    async def console_connect(self, info):
        """Połączenie WebSocket z konsolą (po bilecie z console_ticket)."""
        u = urlsplit(str(self.config["url"]).strip().rstrip("/"))
        query = urlencode({"port": info["port"], "vncticket": info["ticket"]})
        try:
            return await websockets.connect(
                f"wss://{u.netloc}/api2/json{info['path']}/vncwebsocket?{query}", open_timeout=10, max_size=None,
                subprotocols=["binary"], additional_headers={"Authorization": f"PVEAPIToken={self._token()}"},
                ssl=_ssl_context(self.config.get("ca_pem", ""), bool(self.config.get("verify_tls", True))))
        except websockets.InvalidStatus as e:
            server = ", ".join(e.response.headers.get_all("Server")) or "?"
            if e.response.status_code == 200:   # Proxmox przyjął token i bilet, ale nie dostał nagłówka Upgrade
                raise AdapterError("Coś między panelem a Proxmoxem nie przepuszcza WebSocket (nagłówek Upgrade): w adresie "
                                   f"integracji wpisz bezpośrednio https://IP-węzła:8006 (odpowiedział: {server}).") from None
            raise AdapterError(f"Proxmox odrzucił połączenie z konsolą (HTTP {e.response.status_code}, {server}).") from None
        except (OSError, websockets.InvalidHandshake, TimeoutError):
            raise AdapterError("Nie udało się połączyć z konsolą w Proxmoxie.") from None

    async def run_action(self, key, external_id, data, params):
        base = self._guest_path(external_id, data)
        node = data["node"]
        async with self._client() as cli:
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
        async with self._client() as cli:
            st = await self._call(cli, "GET", f"/nodes/{quote(str(task['node']), safe='')}/tasks/"
                                              f"{quote(task['upid'], safe='')}/status") or {}
        if st.get("status") != "stopped":
            return None
        exit_ = str(st.get("exitstatus") or "")
        if exit_ == "OK" or exit_.startswith("WARNINGS"):
            return ActionResult(True, (task.get("text") or "zakończone") + (f" ({exit_})" if exit_ != "OK" else ""))
        return ActionResult(False, f"Proxmox: {exit_ or 'zadanie nie powiodło się'}"[:300])
