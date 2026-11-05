"""Podsumowanie tygodnia na Discord — w poniedziałek od SEND_HOUR, raz na tydzień.

Odpowiada na „jak było” bez zaglądania do panelu: dostępność (z historii stanów), incydenty, co migocze,
co się zbliża (certyfikaty, opłaty, miejsce na dyskach) i ile czeka aktualizacji. Idzie do wszystkich włączonych
kanałów niezależnie od progu ważności — to nie alarm, tylko raport.
"""
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from . import history, notify, upcoming, updates
from .db import kv_get, kv_set
from .models import device, incident, state_change

SEND_WEEKDAY, SEND_HOUR = 0, 8          # poniedziałek, 8:00 czasu serwera
WEEK = 7 * 86400
SENT_KEY = "weekly.sent"
MONTH_KEY = "monthly.sent"


def _duration(seconds):
    m = round(seconds / 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def build(conn, base_url, now=None):
    now = now or time.time()
    since = now - WEEK
    names = {r.id: r.name for r in conn.execute(select(device.c.id, device.c.name))}
    lines = []

    worst = []
    for d in names:
        a = history.availability(conn, d, 168, now)
        if a["pct"] is not None and a["pct"] < 100:
            worst.append((a["pct"], names[d], a["down_s"]))
    worst.sort()
    if worst:
        lines.append("**Dostępność:** " + "; ".join(f"{n} {p}% (nie działało {_duration(s)})" for p, n, s in worst[:5])
                     + (" — reszta 100%" if len(worst) < len(names) else ""))
    else:
        lines.append("**Dostępność:** wszystko 100% (tam, gdzie jest historia)")

    incs = conn.execute(select(incident).where(incident.c.started_at >= since)).mappings().all()
    if incs:
        longest = max(incs, key=lambda i: (i["ended_at"] or now) - i["started_at"])
        lines.append(f"**Incydenty:** {len(incs)} — najdłuższy: {longest['title']} "
                     f"({_duration((longest['ended_at'] or now) - longest['started_at'])})")
    else:
        lines.append("**Incydenty:** żadnych")

    flaps = conn.execute(select(state_change.c.device_id, func.count().label("n")).where(
        state_change.c.ts >= since, state_change.c.state == "down").group_by(state_change.c.device_id)
        .having(func.count() >= 3).order_by(func.count().desc()).limit(3)).all()
    if flaps:
        lines.append("**Migocze:** " + ", ".join(f"{names.get(f.device_id, f.device_id)} ({f.n}× przerwa)" for f in flaps))

    soon = upcoming.items(conn, now)
    if soon:
        lines.append("**Nadchodzące:**\n" + "\n".join(f"• {u['text']}" for u in soon[:5]))

    waiting = updates.count(conn)
    if waiting:
        lines.append(f"**Aktualizacje:** {waiting} urządz. czeka")

    start = datetime.fromtimestamp(since).strftime("%d.%m")
    end = datetime.fromtimestamp(now).strftime("%d.%m")
    return {"username": "Denali", "allowed_mentions": {"parse": []},
            "embeds": [{"title": f"Tydzień {start}–{end}", "url": base_url.rstrip("/") + "/",
                        "description": "\n".join(lines)[:4000], "color": notify.COLORS["info"]}]}


def _month_start(t):
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def build_month(conn, base_url, now=None):
    """Dostępność za poprzedni miesiąc kalendarzowy."""
    now = now or time.time()
    end = _month_start(datetime.fromtimestamp(now))
    start = _month_start(end - timedelta(days=1))
    hours = round((end - start).total_seconds() / 3600)
    names = {r.id: r.name for r in conn.execute(select(device.c.id, device.c.name))}
    worst, full = [], 0
    for d, name in names.items():
        a = history.availability(conn, d, hours, end.timestamp())
        if a["pct"] is None:
            continue
        if a["pct"] < 100:
            worst.append((a["pct"], name, a["down_s"]))
        else:
            full += 1
    worst.sort()
    incs = conn.execute(select(func.count()).select_from(incident).where(
        incident.c.started_at >= start.timestamp(), incident.c.started_at < end.timestamp())).scalar()
    lines = [f"**Incydenty:** {incs}", f"**Bez przerw:** {full} urządz."]
    if worst:
        lines.append("**Najsłabiej:**\n" + "\n".join(f"• {n}: {p}% (nie działało {_duration(s)})" for p, n, s in worst[:10]))
    return {"username": "Denali", "allowed_mentions": {"parse": []},
            "embeds": [{"title": f"Dostępność {start.strftime('%m.%Y')}", "url": base_url.rstrip("/") + "/",
                        "description": "\n".join(lines)[:4000], "color": notify.COLORS["info"]}]}


async def process_month(conn, box, base_url, transport=None, now=None, force=False):
    """Pierwszego dnia miesiąca od SEND_HOUR, raz na miesiąc."""
    now = now or time.time()
    t = datetime.fromtimestamp(now)
    month = t.strftime("%Y-%m")
    if not force and (t.day != 1 or t.hour < SEND_HOUR or kv_get(conn, MONTH_KEY) == month):
        return False
    if not notify.channels(conn, enabled_only=True):
        return False
    sent = await notify.deliver(conn, box, build_month(conn, base_url, now), "krytyczny", transport, now, any_threshold=True)
    if sent and not force:
        kv_set(conn, MONTH_KEY, month)
    return sent


def due(conn, now=None):
    now = now or time.time()
    t = datetime.fromtimestamp(now)
    week = t.strftime("%G-%V")
    return t.weekday() == SEND_WEEKDAY and t.hour >= SEND_HOUR and kv_get(conn, SENT_KEY) != week, week


async def process(conn, box, base_url, transport=None, now=None, force=False):
    """Wysyła podsumowanie, gdy przyszła pora (albo force). True, gdy choć jeden kanał przyjął."""
    now = now or time.time()
    ok, week = due(conn, now)
    if not (ok or force) or not notify.channels(conn, enabled_only=True):
        return False
    sent = await notify.deliver(conn, box, build(conn, base_url, now), "krytyczny", transport, now, any_threshold=True)
    if sent and not force:
        kv_set(conn, SENT_KEY, week)
    return sent
