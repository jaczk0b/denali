"""Szablony Jinja. Każda strona dostaje sesję i token CSRF."""
import hashlib
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlalchemy import func, select

from . import incidents, inventory, live, updates
from .models import external_ref, incident
from .status import initials, tile_color

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
# Wersja plików statycznych w adresie (?v=…): po wdrożeniu przeglądarka bierze nowy CSS/JS, a nie stary z pamięci.
STATIC_V = hashlib.sha256(b"".join((Path(__file__).resolve().parent / "static" / n).read_bytes()
                                   for n in ("denali.css", "denali.js"))).hexdigest()[:10]
# Markdown bez surowego HTML; markdown-it sam odrzuca linki javascript: i data:
# Strony odświeżane na żywo (SSE). Tylko widoki do patrzenia — nie strona po wysłaniu formularza (błąd albo
# odsłonięte hasło zniknęłyby przy podmianie) ani karta urządzenia ze skarbcem.
LIVE_TEMPLATES = {"pulpit.html", "urzadzenia.html", "incydenty.html", "incydent.html", "infra_proxmox.html",
                  "infra_truenas.html", "infra_docker.html", "infra_siec.html", "infra_adresy.html", "infra_mapa.html"}
_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def _dt(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def _hm(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"


def _md_filter(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _bytes(n) -> str:
    """Pojemność po polsku: 5,6 GB · 13,4 TB (przecinek, jak w makiecie)."""
    if not isinstance(n, (int, float)) or n < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            text = f"{n:.0f}" if unit == "B" or n >= 100 else f"{n:.1f}"
            return f"{text.replace('.', ',')} {unit}"
        n /= 1024


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "—"


def _uptime(s) -> str:
    if not isinstance(s, (int, float)) or s <= 0:
        return "—"
    if s >= 86400:
        d = int(s // 86400)
        return f"{d} {'dzień' if d == 1 else 'dni'}"
    if s >= 3600:
        return f"{int(s // 3600)} h"
    return f"{max(1, int(s // 60))} min"


templates.env.filters.update(dt=_dt, hm=_hm, md=_md_filter, initials=initials, tile_color=tile_color, size=_size,
                             bytes=_bytes, pct=_pct, uptime=_uptime)
templates.env.globals.update(KINDS=inventory.KINDS, DEP_TYPES=inventory.DEP_TYPES, VIA=incidents.VIA,
                             version_text=updates.version_text, STATIC_V=STATIC_V,
                             PRIORITY={1: "P1", 2: "P2", 3: "P3"})


def _counts(request: Request) -> dict:
    """Liczniki w nawigacji: otwarte incydenty, urządzenia do aktualizacji i kolejka „do zatwierdzenia”."""
    with request.app.state.engine.connect() as conn:
        return {
            "incidents": conn.execute(select(func.count()).select_from(incident)
                                      .where(incident.c.status != "zamkniety")).scalar(),
            "queue": conn.execute(select(func.count()).select_from(external_ref)
                                  .where(external_ref.c.status == "nowy")).scalar(),
            "updates": updates.count(conn),
        }


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    sess = getattr(request.state, "sess", None)
    signed_in = bool(sess) and sess["kind"] != "pending"
    base = {"sess": sess, "csrf": sess["csrf"] if sess else "", "path": request.url.path,
            "counts": _counts(request) if signed_in else {}, "live_version": ""}
    if signed_in and request.method == "GET" and name in LIVE_TEMPLATES and status == 200:
        with request.app.state.engine.connect() as conn:
            base["live_version"] = live.version(conn)
    return templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status)
