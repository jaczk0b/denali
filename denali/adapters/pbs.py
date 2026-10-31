"""Proxmox Backup Server: ostatnia kopia każdej VM i kontenera (podpięta pod ich karty) i zajętość magazynów.
Tylko odczyt (token z rolą DatastoreAudit / Audit).

Kopia starsza niż MAX_AGE_H godzin = „uwaga” na karcie VM (to nie awaria — maszyna działa, tylko bez świeżej kopii).
Aktualizacje samego PBS: `/nodes/localhost/apt/update` (jak w jego panelu: Administration → Updates).
"""
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from . import Adapter, AdapterError, Field, Found, PollResult, State, apt_updates
from .tls import ssl_context, validate_pem_field

CA_HINT = "cat /etc/proxmox-backup/proxy.pem"
MAX_AGE_H = 36
USAGE_WARN = 0.85


def _when(ts):
    if not ts:
        return "nigdy"
    d = datetime.fromtimestamp(ts)
    return d.strftime("dziś %H:%M") if d.date() == datetime.now().date() else d.strftime("%d.%m %H:%M")


class Pbs(Adapter):
    type = "pbs"
    label = "Proxmox Backup Server"
    default_poll = 300
    fields = [
        Field("url", "Adres", "url", help="np. https://192.168.1.15:8007 — bez ścieżki"),
        Field("token_id", "Identyfikator tokenu", help="użytkownik@realm!nazwa, np. denali@pbs!odczyt"),
        Field("token_secret", "Sekret tokenu", "secret"),
        Field("ca_pem", "Certyfikat PBS (proxy.pem)", "textarea", required=False,
              help="Zawartość /etc/proxmox-backup/proxy.pem (sam certyfikat, bez klucza)."),
        Field("verify_tls", "Sprawdzaj certyfikat", "bool", required=False, default=True),
    ]

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip().rstrip("/"))
        if p.scheme != "https" or not p.hostname or p.path or p.query or p.username:
            raise AdapterError("Adres: https://IP-albo-nazwa:8007, bez ścieżki.")
        if "@" not in str(config.get("token_id", "")) or "!" not in str(config.get("token_id", "")):
            raise AdapterError("Identyfikator tokenu ma postać użytkownik@realm!nazwa, np. denali@pbs!odczyt.")
        validate_pem_field(config, CA_HINT)

    def _client(self):
        url = str(self.config["url"]).strip().rstrip("/")
        # w PBS sekret tokenu oddziela dwukropek (w Proxmoxie VE — znak „=”)
        headers = {"Authorization": f"PBSAPIToken={self.config['token_id'].strip()}:{self.secrets['token_secret'].strip()}",
                   "Accept": "application/json"}
        kw = {"transport": self.transport} if self.transport else {
            "verify": ssl_context(self.config.get("ca_pem", ""), bool(self.config.get("verify_tls", True)), CA_HINT)}
        return httpx.AsyncClient(base_url=url + "/api2/json", headers=headers, timeout=15, follow_redirects=False, **kw)

    async def _get(self, cli, path):
        try:
            r = await cli.get(path)
        except httpx.ConnectError as e:
            if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
                raise AdapterError("Certyfikat PBS nie jest zaufany — wklej proxy.pem albo wyłącz sprawdzanie.") from None
            raise AdapterError(f"Brak połączenia z {self.config['url']} — sprawdź adres i czy PBS działa.") from None
        except httpx.HTTPError as e:
            raise AdapterError(f"Błąd połączenia: {e.__class__.__name__}") from None
        if r.status_code == 401:
            raise AdapterError("PBS odrzucił token — sprawdź identyfikator i sekret.")
        if r.status_code == 403:
            raise AdapterError(f"Token nie ma uprawnień do {path} — nadaj mu rolę Audit na „/”.")
        if r.status_code >= 400:
            raise AdapterError(f"PBS zwrócił błąd {r.status_code} dla {path}.")
        try:
            return r.json().get("data")
        except ValueError:
            raise AdapterError("Odpowiedź nie wygląda na API PBS — sprawdź adres i port (zwykle 8007).") from None

    async def _collect(self):
        async with self._client() as cli:
            stores = [s.get("store") for s in (await self._get(cli, "/admin/datastore") or []) if s.get("store")]
            try:
                usage = {u.get("store"): u for u in (await self._get(cli, "/status/datastore-usage") or [])}
            except AdapterError:
                usage = {}
            groups = {}
            for store in stores:
                groups[store] = await self._get(cli, f"/admin/datastore/{store}/groups") or []
            try:
                self._apt = await self._get(cli, "/nodes/localhost/apt/update") or []
            except AdapterError:
                self._apt = None    # token bez Sys.Audit — lista aktualizacji zostaje bez zmian
        return stores, usage, groups

    async def test(self):
        stores, _usage, groups = await self._collect()
        if not stores:
            raise AdapterError("Połączono, ale token nie widzi żadnego magazynu — nadaj mu rolę Audit "
                               "(albo DatastoreAudit) na „/”.")
        n = sum(1 for g in groups.values() for x in g if x.get("backup-type") in ("vm", "ct"))
        return f"Połączono: magazyny: {len(stores)} · kopie VM i kontenerów: {n}"

    async def poll(self):
        now = time.time()
        stores, usage, groups = await self._collect()
        out = PollResult()
        if self._apt is not None:
            out.updates = apt_updates("server", self._apt)
        host = urlsplit(str(self.config["url"])).hostname or "pbs"
        ip = host if host.replace(".", "").isdigit() else None
        out.found.append(Found("server", {"name": "PBS" if ip else host, "kind": "vm",
                                          "addresses": [{"kind": "ip", "value": ip}] if ip else []}))
        parts, worst = [], "ok"
        for store in stores:
            u = usage.get(store) or {}
            if u.get("total"):
                pct = u.get("used", 0) / u["total"]
                parts.append(f"{store} {pct:.0%}")
                if pct >= USAGE_WARN:
                    worst = "warn"
            else:
                parts.append(store)
        out.states.append(State("server", worst, "magazyny: " + ", ".join(parts) if parts else "brak magazynów",
                                {"stores": stores}))
        latest = {}
        for store, items in groups.items():
            for g in items:
                typ, bid = g.get("backup-type"), str(g.get("backup-id", ""))
                if typ not in ("vm", "ct") or not bid.isdigit():
                    continue
                key = (typ, bid)
                prev = latest.get(key)
                if not prev or (g.get("last-backup") or 0) > (prev[1].get("last-backup") or 0):
                    latest[key] = (store, g)
        for (typ, bid), (store, g) in sorted(latest.items()):
            last = g.get("last-backup")
            age_h = (now - last) / 3600 if last else None
            fresh = age_h is not None and age_h <= MAX_AGE_H
            summary = f"ostatnia kopia: {_when(last)} · kopii: {g.get('backup-count', '?')} · magazyn {store}"
            if not fresh:
                summary = (f"brak świeżej kopii — ostatnia {_when(last)}" if last else "brak kopii") + f" · magazyn {store}"
            ids = [f"qemu/{bid}"] if typ == "vm" else [f"lxc/{bid}"]
            out.states.append(State(f"backup/{typ}/{bid}", "ok" if fresh else "warn", summary,
                                    {"last_backup": last, "count": g.get("backup-count"), "store": store},
                                    attach={"integration_type": "proxmox", "external_ids": ids},
                                    label=f"kopie {'VM' if typ == 'vm' else 'kontenera'} {bid}"))
        return out
