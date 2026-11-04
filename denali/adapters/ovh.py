"""OVHcloud: VPS-y z konta — stan maszyny, plan, adresy, kopie i termin opłaty. Tylko odczyt (token z samymi GET-ami).

VPS trafia do kolejki „do zatwierdzenia” jak każdy obiekt; po zatwierdzeniu dostaje terminal SSH z karty Terminale.
OVH mówi, czy maszyna jest włączona — to, czy usługa odpowiada, sprawdzają zwykłe sprawdzenia z adresu urządzenia.

Termin opłaty: przy odnowieniu automatycznym nic nie trzeba robić, więc „uwaga” dopiero przy problemie; przy ręcznym
„uwaga” na WARN_DAYS dni przed końcem. Zaległości i faktury są na poziomie konta (stan „konto”, bez urządzenia) — token
bez tych uprawnień działa dalej, tylko zakładka VPS ich nie pokaże.
"""
import hashlib
import time
from datetime import date, timedelta
from urllib.parse import quote, urlencode

import httpx

from . import Adapter, AdapterError, Field, Found, PollResult, State

ENDPOINTS = {"ovh-eu": "https://eu.api.ovh.com/1.0", "ovh-ca": "https://ca.api.ovh.com/1.0",
             "ovh-us": "https://api.us.ovhcloud.com/1.0"}
MANAGER = {"ovh-eu": "https://www.ovh.com/manager/", "ovh-ca": "https://ca.ovh.com/manager/"}
WARN_DAYS = 14
BILLS = 3
BILLS_DAYS = 100
STATES = {"running": ("ok", "działa"), "stopped": ("down", "wyłączony"), "stopping": ("warn", "wyłącza się"),
          "rebooting": ("warn", "uruchamia się ponownie"), "installing": ("warn", "instalacja systemu"),
          "upgrading": ("warn", "zmiana planu"), "maintenance": ("warn", "prace serwisowe OVH"),
          "backuping": ("warn", "kopia w toku"), "rescued": ("warn", "tryb ratunkowy")}
WORST = ("down", "warn", "ok")


class _Unavailable(AdapterError):
    """Token nie ma uprawnień do tego adresu albo obiektu nie ma (np. VPS bez snapshotu)."""


def _day(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def payment(service, today=None):
    """(stan, opis) z /vps/<nazwa>/serviceInfos."""
    today = today or date.today()
    status, renew = service.get("status"), service.get("renew") or {}
    end = _day(service.get("expiration"))
    if not end:
        return "warn", "OVH nie podał terminu opłaty"
    days, when = (end - today).days, end.strftime("%d.%m.%Y")
    auto = bool(renew.get("automatic")) and not renew.get("manualPayment")
    if status == "autorenewInProgress":
        return "ok", f"odnowienie w toku (termin {when})"
    if status == "expired" or days < 0:
        return "down", f"wygasł {when}"
    if status in ("unPaid", "pendingDebt"):
        return "warn", f"nieopłacony, termin {when}"
    if renew.get("deleteAtExpiration"):
        return ("warn" if days <= WARN_DAYS else "ok"), f"zostanie usunięty {when} (za {days} dni)"
    if auto:
        return "ok", f"odnowienie automatyczne {when}"
    return ("warn" if days <= WARN_DAYS else "ok"), f"zapłać do {when} (za {days} dni)"


class Ovh(Adapter):
    type = "ovh"
    label = "OVHcloud (VPS)"
    default_poll = 300
    removable = ("vps/",)
    fields = [
        Field("endpoint", "Region API", required=False, default="ovh-eu", help="ovh-eu, ovh-ca albo ovh-us"),
        Field("application_key", "Application Key"),
        Field("application_secret", "Application Secret", "secret"),
        Field("consumer_key", "Consumer Key", "secret"),
    ]

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        if cls._endpoint(config) not in ENDPOINTS:
            raise AdapterError("Region API: ovh-eu, ovh-ca albo ovh-us.")

    @staticmethod
    def _endpoint(config):
        return str(config.get("endpoint") or "ovh-eu").strip()

    def _client(self):
        kw = {"transport": self.transport} if self.transport else {}
        return httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"Accept": "application/json"}, **kw)

    async def _send(self, cli, url, headers=None):
        try:
            return await cli.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise AdapterError(f"Brak połączenia z API OVH ({e.__class__.__name__}).") from None

    async def _clock(self, cli):
        """Różnica czasu względem OVH: podpis z przestarzałym czasem jest odrzucany."""
        r = await self._send(cli, ENDPOINTS[self._endpoint(self.config)] + "/auth/time")
        try:
            r.raise_for_status()
            return int(r.json()) - int(time.time())
        except (httpx.HTTPError, ValueError, TypeError):
            raise AdapterError("Odpowiedź nie wygląda na API OVH.") from None

    async def _get(self, cli, path, params=None):
        url = ENDPOINTS[self._endpoint(self.config)] + path + (f"?{urlencode(params)}" if params else "")
        app_key = str(self.config["application_key"]).strip()
        secret, consumer = self.secrets["application_secret"].strip(), self.secrets["consumer_key"].strip()
        ts = str(int(time.time()) + self._delta)
        sign = hashlib.sha1(f"{secret}+{consumer}+GET+{url}++{ts}".encode()).hexdigest()
        r = await self._send(cli, url, {"X-Ovh-Application": app_key, "X-Ovh-Consumer": consumer,
                                        "X-Ovh-Timestamp": ts, "X-Ovh-Signature": f"$1${sign}"})
        if r.status_code < 400:
            try:
                return r.json()
            except ValueError:
                raise AdapterError("Odpowiedź nie wygląda na API OVH.") from None
        try:
            code = r.json().get("errorCode", "")
        except (ValueError, AttributeError):
            code = ""
        if r.status_code == 404 or code == "NOT_GRANTED_CALL":
            raise _Unavailable(f"Token nie ma dostępu do {path} — załóż nowy z uprawnieniami z instrukcji.")
        if r.status_code in (400, 401, 403):
            raise AdapterError("OVH odrzuciło klucze — sprawdź Application Key, Application Secret i Consumer Key.")
        raise AdapterError(f"API OVH zwróciło błąd {r.status_code} dla {path}.")

    async def _optional(self, cli, path, params=None):
        try:
            return await self._get(cli, path, params)
        except _Unavailable:
            return None

    async def _account(self, cli):
        """Zaległości i ostatnie faktury; None = token tego nie widzi."""
        debt = await self._optional(cli, "/me/debtAccount")
        since = (date.today() - timedelta(days=BILLS_DAYS)).isoformat()
        ids = await self._optional(cli, "/me/bill", {"date.from": since})
        if debt is None and ids is None:
            return None
        bills = []
        for bill_id in sorted(ids or [])[-BILLS:]:
            b = await self._optional(cli, f"/me/bill/{quote(str(bill_id), safe='')}")
            if b:   # bez adresów PDF: zawierają jednorazowy klucz do faktury
                bills.append({"id": b.get("billId") or bill_id, "date": str(b.get("date") or "")[:10],
                              "amount": (b.get("priceWithTax") or {}).get("text") or ""})
        due = (debt or {}).get("dueAmount") or {}
        return {"due": due.get("text") or "", "due_value": due.get("value") or 0,
                "bills": sorted(bills, key=lambda b: b["date"], reverse=True)}

    async def _vps(self, cli, name):
        base = f"/vps/{quote(name, safe='')}"
        info = await self._get(cli, base)
        service = await self._get(cli, f"{base}/serviceInfos")
        options = await self._optional(cli, f"{base}/option") or []
        return {"info": info, "service": service, "options": options,
                "ips": await self._optional(cli, f"{base}/ips") or [],
                "datacenter": await self._optional(cli, f"{base}/datacenter") or {},
                "snapshot": await self._optional(cli, f"{base}/snapshot") if "snapshot" in options else None,
                "backup": await self._optional(cli, f"{base}/automatedBackup") if "automatedBackup" in options else None}

    async def _collect(self):
        async with self._client() as cli:
            self._delta = await self._clock(cli)
            names = await self._get(cli, "/vps")
            return [(n, await self._vps(cli, n)) for n in names], await self._account(cli)

    async def test(self):
        vpses, account = await self._collect()
        if not vpses:
            raise AdapterError("Połączono, ale na tym koncie nie ma VPS-ów (Public Cloud nie jest obsługiwany).")
        return f"Połączono: VPS: {len(vpses)} · rachunki: {'widoczne' if account else 'brak uprawnień'}"

    async def poll(self):
        vpses, account = await self._collect()
        out = PollResult()
        for name, v in vpses:
            eid, info, service = f"vps/{name}", v["info"], v["service"]
            model = info.get("model") or {}
            ips = sorted(map(str, v["ips"]), key=lambda ip: (":" in ip, ip))
            addresses = [{"kind": "ip", "value": ip} for ip in ips] + [{"kind": "hostname", "value": name}]
            title = info.get("displayName") or name
            out.found.append(Found(eid, {"name": title, "kind": "vps", "addresses": addresses}))
            state, text = STATES.get(info.get("state"), ("warn", str(info.get("state") or "stan nieznany")))
            pay_state, pay_text = payment(service)
            lock = info.get("lockStatus") or {}
            final = "warn" if lock.get("locked") and state == "ok" else state
            final = next(w for w in WORST if w in (final, pay_state))
            snapshot, backup = v["snapshot"] or {}, v["backup"] or {}
            out.states.append(State(eid, final, f"{text} · {pay_text}", {
                "name": name, "state": info.get("state"), "state_text": text, "offer": model.get("offer") or "",
                "plan": model.get("name") or "", "vcore": info.get("vcore") or model.get("vcore"),
                "memory_mb": info.get("memoryLimit") or model.get("memory"), "disk_gb": model.get("disk"),
                "location": v["datacenter"].get("longName") or info.get("zone") or "", "ips": ips,
                "locked": lock.get("reason") or "" if lock.get("locked") else "", "payment_state": pay_state,
                "payment": pay_text, "expiration": str(service.get("expiration") or "")[:10],
                "created": str(service.get("creation") or "")[:10],
                "engaged_to": str(service.get("engagedUpTo") or "")[:10],
                "contact_billing": service.get("contactBilling") or "", "options": v["options"],
                "backup": backup.get("state") == "enabled", "backup_at": backup.get("schedule") or "",
                "snapshot_at": str(snapshot.get("creationDate") or "")[:16].replace("T", " ")}))
        if account:
            due = account["due_value"]
            out.states.append(State("konto", "warn" if due else "ok",
                                    f"zaległość {account['due']}" if due else "bez zaległości", account))
        return out
