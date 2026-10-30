"""Router TP-Link na oryginalnym oprogramowaniu (np. Archer AX23, AX10) — przez NIEOFICJALNĄ bibliotekę
tplinkrouterc6u. Tylko odczyt: stan routera i lista podłączonych klientów.

Ograniczenia (świadoma decyzja z 30.10.2026; docelowo OpenWrt — patrz POMYSLY.md):
- wymaga hasła administratora routera (TP-Link nie ma kont tylko do odczytu) — trzymane zaszyfrowane,
- przy każdym odczycie biblioteka loguje się jako admin; po odczycie zawsze się wylogowujemy, żeby nie blokować
  panelu routera w przeglądarce (TP-Link pozwala na jedną sesję administratora naraz),
- aktualizacja firmware'u TP-Linka może zepsuć logowanie — wtedy integracja pokaże błąd, a ping dalej działa.

Klienci sieci trafiają do kolejki „do zatwierdzenia” (= nowe urządzenie w sieci); znane dopasują się po MAC.
Każdy klient dostaje zależność „sieć” od tego routera / AP: przez Wi-Fi twardą (padnie AP = klient też),
przez kabel miękką (za routerem zwykle jest switch — ping w LAN działa dalej). Telefon, który przejdzie do innego
AP, przy następnym odczycie zależy od nowego.
"""
import asyncio
import re
from urllib.parse import urlsplit

from . import Adapter, AdapterError, Field, Found, PollResult, State

PHONE = re.compile(r"iphone|ipad|android|galaxy|pixel|redmi|xiaomi|huawei|oneplus|phone|telefon", re.I)
BAND = {"HOST_2G": "Wi-Fi 2,4 GHz", "HOST_5G": "Wi-Fi 5 GHz", "HOST_6G": "Wi-Fi 6 GHz", "GUEST_2G": "Wi-Fi gości 2,4 GHz",
        "GUEST_5G": "Wi-Fi gości 5 GHz", "GUEST_6G": "Wi-Fi gości 6 GHz", "IOT_2G": "Wi-Fi IoT 2,4 GHz",
        "IOT_5G": "Wi-Fi IoT 5 GHz", "WIRED": "kabel"}


def _mac(v):
    return str(v or "").strip().lower().replace("-", ":")


def _read(url, username, password, verify, timeout=20):
    """Synchroniczny odczyt przez bibliotekę (uruchamiany w osobnym wątku). Zawsze kończy wylogowaniem."""
    from tplinkrouterc6u import AuthorizeError, ClientException, TplinkRouterProvider
    try:
        router = TplinkRouterProvider.get_client(url, password, username or "admin", verify_ssl=verify, timeout=timeout)
    except AuthorizeError:
        raise AdapterError("Router odrzucił hasło (albo ten model nie jest obsługiwany przez bibliotekę).") from None
    except ClientException as e:
        raise AdapterError(f"Nie udało się połączyć z routerem: {str(e)[:150]}") from None
    except Exception as e:      # biblioteka zgłasza też zwykłe błędy sieci (requests)
        raise AdapterError(f"Brak połączenia z {url} ({e.__class__.__name__}).") from None
    try:
        router.authorize()
        status = router.get_status()
        try:
            fw = router.get_firmware()
        except Exception:
            fw = None
        return status, fw
    except AuthorizeError:
        raise AdapterError("Router odrzucił hasło.") from None
    except ClientException as e:
        raise AdapterError(f"Router zwrócił błąd: {str(e)[:150]}") from None
    except Exception as e:
        raise AdapterError(f"Odczyt z routera nie powiódł się ({e.__class__.__name__}).") from None
    finally:
        try:
            router.logout()
        except Exception:
            pass


class Tplink(Adapter):
    type = "tplink"
    label = "Router TP-Link (nieoficjalnie)"
    default_poll = 300
    fields = [
        Field("url", "Adres routera", "url", help="np. http://192.168.1.1 (tak jak otwierasz panel routera)"),
        Field("username", "Login", required=False, default="admin", help="w większości modeli: admin (albo puste)"),
        Field("password", "Hasło administratora routera", "secret"),
        Field("as_ap", "To urządzenie działa jako punkt dostępowy (AP), nie główny router", "bool", required=False,
              default=False),
        Field("verify_tls", "Sprawdzaj certyfikat (tylko dla https)", "bool", required=False, default=False),
    ]
    reader = staticmethod(_read)       # w testach: podmiana odczytu (bez prawdziwego routera)

    @classmethod
    def validate(cls, config, secrets):
        super().validate(config, secrets)
        p = urlsplit(str(config.get("url", "")).strip())
        if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/") or p.query:
            raise AdapterError("Adres: http://IP-routera (bez ścieżki).")

    async def _status(self):
        url = str(self.config["url"]).strip().rstrip("/")
        return await asyncio.to_thread(self.reader, url, str(self.config.get("username") or "admin"),
                                       self.secrets["password"], bool(self.config.get("verify_tls", False)))

    async def test(self):
        status, fw = await self._status()
        hw = (fw.hardware_version or "").strip() if fw else ""
        model = (hw if hw.startswith(fw.model) else f"{fw.model} {hw}".strip()) if fw else "router TP-Link"
        return f"Połączono: {model} · klienci: {getattr(status, 'clients_total', '?')}"

    async def poll(self):
        status, fw = await self._status()
        out = PollResult()
        model = (getattr(fw, "model", "") or "TP-Link").strip()
        lan_ip = str(getattr(status, "lan_ipv4_addr", "") or "")
        lan_mac = _mac(getattr(status, "lan_macaddr", ""))
        addrs = [a for a in ([{"kind": "ip", "value": lan_ip}] if lan_ip else []) +
                 ([{"kind": "mac", "value": lan_mac}] if lan_mac else [])]
        kind = "ap" if self.config.get("as_ap") else "router"
        out.found.append(Found("router", {"name": model, "kind": kind, "addresses": addrs}))
        wifi, wired = getattr(status, "wifi_clients_total", None), getattr(status, "wired_total", None)
        hw = (getattr(fw, "hardware_version", "") or "").strip()
        parts = [hw if hw.startswith(model) else f"{model} {hw}".strip(),
                 f"klienci: {getattr(status, 'clients_total', '?')}"
                 + (f" (Wi-Fi {wifi}, kabel {wired})" if wifi is not None and wired is not None else "")]
        if getattr(status, "cpu_usage", None) is not None:
            parts.append(f"CPU {status.cpu_usage:.0%}" if status.cpu_usage <= 1 else f"CPU {status.cpu_usage:.0f}%")
        if not self.config.get("as_ap") and getattr(status, "wan_ipv4_addr", None):
            parts.append(f"WAN {status.wan_ipv4_addr}")
        out.states.append(State("router", "ok", " · ".join(parts),
                                {"model": model, "firmware": getattr(fw, "firmware_version", None),
                                 "clients": getattr(status, "clients_total", None)}))
        for d in getattr(status, "devices", None) or []:
            mac = _mac(getattr(d, "macaddr", ""))
            if not mac or mac == lan_mac:
                continue
            ip, host = str(getattr(d, "ipaddr", "") or ""), (getattr(d, "hostname", "") or "").strip()
            conn = getattr(getattr(d, "type", None), "name", "") or ""
            addrs = [{"kind": "mac", "value": mac}] + ([{"kind": "ip", "value": ip}] if ip and ip != "0.0.0.0" else [])
            out.found.append(Found(f"mac/{mac}", {
                "name": host if host and host not in ("--", "Unknown") else mac,
                "kind": "telefon" if PHONE.search(host) else "komputer", "addresses": addrs,
                "uplink": {"ref": "router", "hard": conn != "WIRED"}}))
            how = BAND.get(conn, "sieć")
            signal = getattr(d, "signal", None)
            summary = f"w sieci · {how}" + (f" · sygnał {signal} dBm" if signal else "") + (f" · {ip}" if ip else "")
            out.states.append(State(f"mac/{mac}", "ok" if getattr(d, "active", True) else "unknown", summary,
                                    {"connection": conn, "signal": signal}))
        return out
