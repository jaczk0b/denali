"""Strażnik: Denali co minutę melduje się zewnętrznemu monitorowi na VPS (Uptime Kuma „Push” albo healthchecks.io).
Gdy meldunki ustaną (padł panel, VM, pve, prąd albo internet w domu), monitor na VPS sam powiadamia na Discord.

Adres meldunku zawiera sekretny token — trzymany zaszyfrowany. Meldunek wysyła worker, więc „cisza” obejmuje też
sytuację, w której panel WWW działa, ale worker (stan, incydenty, powiadomienia) stoi.
"""
import time
from urllib.parse import urlsplit

import httpx

from .db import kv_get, kv_set

KEY = "watchdog"
EVERY = 60


class WatchdogError(ValueError):
    pass


def validate_url(url):
    u = urlsplit((url or "").strip())
    if u.scheme != "https" or not u.hostname:
        raise WatchdogError("Podaj adres https:// z monitora (Uptime Kuma: monitor „Push” → adres; healthchecks.io: ping URL).")
    return url.strip()


def configure(conn, box, url):
    """Zapisuje adres (pusty = wyłącza strażnika)."""
    if not (url or "").strip():
        kv_set(conn, KEY, {})
        return
    kv_set(conn, KEY, {"url_ct": box.encrypt(validate_url(url)), "last_ok": None, "last_error": ""})


def status(conn):
    data = kv_get(conn, KEY) or {}
    return {"enabled": bool(data.get("url_ct")), "last_ok": data.get("last_ok"), "last_error": data.get("last_error", "")}


async def ping(conn, box, transport=None, now=None, force=False):
    """Wysyła meldunek, jeśli strażnik jest włączony i minęła minuta. Zwraca True po udanym meldunku."""
    now = now or time.time()
    data = dict(kv_get(conn, KEY) or {})
    if not data.get("url_ct"):
        return False
    if not force and now - (data.get("last_attempt") or 0) < EVERY:
        return False
    data["last_attempt"] = now
    url = box.decrypt(data["url_ct"])
    kw = {"transport": transport} if transport else {}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, **kw) as cli:
            r = await cli.get(url, params={"status": "up", "msg": "OK"} if "/api/push/" in url else None)
        if r.status_code >= 400:
            raise WatchdogError(f"monitor zwrócił błąd {r.status_code}")
    except (httpx.HTTPError, WatchdogError) as e:
        data["last_error"] = str(e)[:200] if isinstance(e, WatchdogError) else f"brak połączenia ({e.__class__.__name__})"
        kv_set(conn, KEY, data)
        return False
    data.update(last_ok=now, last_error="")
    kv_set(conn, KEY, data)
    return True

