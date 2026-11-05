"""Producent po adresie MAC: lista IEEE pobierana raz na miesiąc do katalogu danych.

Bez listy (np. brak internetu przy pierwszym uruchomieniu) po prostu nie ma producenta, nic się nie psuje.
Adresy losowe (telefony) nie mają producenta i tak być powinno.
"""
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("denali.oui")
URL = "https://standards-oui.ieee.org/oui/oui.txt"
REFRESH_EVERY = 30 * 86400
RETRY_EVERY = 86400
LINE = re.compile(r"^([0-9A-Fa-f]{6})\s+\(base 16\)\s+(.+?)\s*$")
_cache = {"path": None, "mtime": 0, "map": {}}


def _path(data_dir):
    return Path(data_dir) / "oui.txt"


def parse(text):
    out = {}
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            out[m.group(1).upper()] = m.group(2)
    return out


def lookup(data_dir, mac):
    """Nazwa producenta albo ""."""
    p = _path(data_dir)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ""
    if _cache["path"] != p or _cache["mtime"] != mtime:
        _cache.update(path=p, mtime=mtime, map=parse(p.read_text(encoding="utf-8", errors="replace")))
    return _cache["map"].get(re.sub(r"[^0-9A-Fa-f]", "", mac or "")[:6].upper(), "")


async def refresh(data_dir, state, transport=None, now=None):
    """Pobiera listę, gdy jej nie ma albo jest stara. state: pamięć workera (kiedy była ostatnia próba)."""
    now = now or time.time()
    p = _path(data_dir)
    if p.exists() and now - p.stat().st_mtime < REFRESH_EVERY:
        return False
    if now - state.get("oui_tried_at", 0) < RETRY_EVERY:
        return False
    state["oui_tried_at"] = now
    kw = {"transport": transport} if transport else {}
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers={"User-Agent": "Denali"}, **kw) as cli:
            r = await cli.get(URL)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.info("Lista producentów MAC niepobrana: %s", e.__class__.__name__)
        return False
    if not parse(r.text):
        return False
    tmp = p.with_suffix(".tmp")
    tmp.write_text(r.text, encoding="utf-8")
    tmp.replace(p)
    return True
