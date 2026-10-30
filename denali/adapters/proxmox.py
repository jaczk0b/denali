"""Proxmox VE: węzły, VM i kontenery LXC — stan, zasoby, adresy IP. Tylko odczyt (token z rolą PVEAuditor).

Zatrzymana maszyna to „off” (wyłączona celowo), chyba że ma startować z hostem (onboot) — wtedy „down”.
"""
import asyncio
import re
import ssl
from urllib.parse import urlsplit

import httpx

from . import Adapter, AdapterError, Field, Found, PollResult, State

GB = 1024 ** 3
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----")


def clean_pem(text: str) -> str:
    """Z wklejonego tekstu wyciąga same bloki certyfikatu (np. bez linii „root@pve:~# cat …” z konsoli)."""
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        return ""
    blocks = _PEM.findall(text)
    if not blocks:
        raise AdapterError("Certyfikat CA nie wygląda poprawnie — wklej całość od -----BEGIN CERTIFICATE----- "
                           "do -----END CERTIFICATE----- (wynik: cat /etc/pve/pve-root-ca.pem).")
    return "\n".join(b.strip() for b in blocks) + "\n"


def _ssl_context(ca_pem: str, verify: bool):
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    pem = clean_pem(ca_pem)
    try:
        ctx = ssl.create_default_context(cadata=pem) if pem else ssl.create_default_context()
    except (ssl.SSLError, ValueError):
        raise AdapterError("Certyfikat CA jest uszkodzony — skopiuj go jeszcze raz w całości "
                           "(cat /etc/pve/pve-root-ca.pem).") from None
    # pve-root-ca.pem nie ma rozszerzenia keyUsage, które Python 3.13+ wymaga w trybie ścisłym (tak samo w HLC).
    # Łańcuch, podpis i nazwa hosta są dalej sprawdzane.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


def _days(seconds):
    d = int(seconds or 0) // 86400
    return f"{d} {'dzień' if d == 1 else 'dni'}" if d else "od niedawna"


def _ram(mem, maxmem):
    if not maxmem:
        return ""
    return f"RAM {mem / GB:.1f}/{maxmem / GB:.0f} GB".replace(".", ",")


class Proxmox(Adapter):
    type = "proxmox"
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

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip().rstrip("/"))
        if p.scheme != "https" or not p.hostname or p.path or p.query or p.username:
            raise AdapterError("Adres: https://IP-albo-nazwa:8006, bez ścieżki.")
        tid = str(config.get("token_id", "")).strip()
        if "@" not in tid or "!" not in tid:
            raise AdapterError("Identyfikator tokenu ma postać użytkownik@realm!nazwa, np. denali@pve!odczyt.")
        if config.get("ca_pem"):
            config["ca_pem"] = clean_pem(config["ca_pem"])          # zapisujemy już oczyszczony
            _ssl_context(config["ca_pem"], True)                     # zły certyfikat → błąd na formularzu

    def _client(self):
        url = str(self.config["url"]).strip().rstrip("/")
        headers = {"Authorization": f"PVEAPIToken={self.config['token_id'].strip()}={self.secrets['token_secret'].strip()}",
                   "Accept": "application/json"}
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
        return f"Połączono: Proxmox VE {ver.get('version', '?')} · węzły: {nodes} · maszyny i kontenery: {guests}"

    async def _guest_ips(self, cli, g):
        node, vmid, typ = g["node"], g["vmid"], g["type"]
        ips = []
        try:
            if typ == "qemu":
                data = await self._get(cli, f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces", timeout=5) or {}
                for iface in data.get("result", []):
                    if iface.get("name", "") == "lo" or iface.get("name", "").startswith(("docker", "br-", "veth")):
                        continue
                    ips += [a["ip-address"] for a in iface.get("ip-addresses", [])
                            if a.get("ip-address-type") == "ipv4" and not a.get("ip-address", "").startswith("127.")]
            else:
                for iface in await self._get(cli, f"/nodes/{node}/lxc/{vmid}/interfaces", timeout=5) or []:
                    ip = (iface.get("inet") or "").split("/")[0]
                    if iface.get("name") != "lo" and ip and not ip.startswith("127."):
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

        out = PollResult()
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
