"""Szablony Jinja. Każda strona dostaje sesję i token CSRF."""
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlalchemy import func, select

from . import incidents, inventory
from .models import external_ref, incident
from .status import initials, tile_color

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
# Markdown bez surowego HTML; markdown-it sam odrzuca linki javascript: i data:
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


templates.env.filters.update(dt=_dt, hm=_hm, md=_md_filter, initials=initials, tile_color=tile_color, size=_size)
templates.env.globals.update(KINDS=inventory.KINDS, DEP_TYPES=inventory.DEP_TYPES, VIA=incidents.VIA,
                             PRIORITY={1: "P1", 2: "P2", 3: "P3"})


def _counts(request: Request) -> dict:
    """Liczniki w nawigacji: otwarte incydenty i kolejka „do zatwierdzenia”."""
    with request.app.state.engine.connect() as conn:
        return {
            "incidents": conn.execute(select(func.count()).select_from(incident)
                                      .where(incident.c.status != "zamkniety")).scalar(),
            "queue": conn.execute(select(func.count()).select_from(external_ref)
                                  .where(external_ref.c.status == "nowy")).scalar(),
        }


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    sess = getattr(request.state, "sess", None)
    signed_in = bool(sess) and sess["kind"] != "pending"
    base = {"sess": sess, "csrf": sess["csrf"] if sess else "", "path": request.url.path,
            "counts": _counts(request) if signed_in else {}}
    return templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status)
