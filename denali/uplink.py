"""Czy dom ma internet: co pół minuty próba połączenia z trzema publicznymi serwerami DNS.

Dwie porażki z rzędu = brak internetu. Wtedy to, co zależy od internetu (VPS, adresy publiczne), przechodzi w „nie da
się sprawdzić” zamiast w fałszywą awarię, a zamiast lawiny jest jeden alert na routerze. Powiadomienie i tak nie
wyjdzie, gdy łącza nie ma, więc panel wysyła je po powrocie: ile trwała przerwa.
"""
import asyncio
import time

from .db import kv_get, kv_set

KEY = "uplink"
HOSTS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
PORT = 53
TIMEOUT = 3
FAIL_AFTER = 2


async def _reach(host):
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, PORT), TIMEOUT)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


async def online(reach=None):
    results = await asyncio.gather(*((reach or _reach)(h) for h in HOSTS))
    return any(results)


def record(conn, ok, now=None):
    now = now or time.time()
    if ok:
        kv_set(conn, KEY, {"fails": 0, "down_since": None, "last_ok": now})
        return
    data = dict(kv_get(conn, KEY) or {})
    data["fails"] = int(data.get("fails") or 0) + 1
    if data["fails"] >= FAIL_AFTER and not data.get("down_since"):
        data["down_since"] = now
    kv_set(conn, KEY, data)


def is_down(conn):
    return bool((kv_get(conn, KEY) or {}).get("down_since"))
