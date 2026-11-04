"""Historia: zmiany stanu urządzeń i czasy odpowiedzi sprawdzeń z LAN.

- state_change: wpis tylko przy zmianie stanu (worker co minutę porównuje z poprzednim obiegiem) — z tego pasek
  dostępności na karcie urządzenia i wykrywanie migotania (live.py),
- check_sample: jedna próbka sprawdzenia na SAMPLE_EVERY — wykres czasu odpowiedzi,
- prune: starsze niż KEEP_* dni znikają (raz na godzinę, w obiegu workera).
Bez osobnej bazy metryk — to ma odpowiedzieć na „od kiedy to się sypie”, nie zastąpić Grafany.
"""
import time

from sqlalchemy import delete, func, select

from .models import check_sample, state_change

SAMPLE_EVERY = 300
KEEP_SAMPLES_DAYS = 30
KEEP_CHANGES_DAYS = 180
BAR_SLOTS = 48              # pasek dostępności: 48 odcinków (24 h po 30 min, 7 dni po 3,5 h)
WORST = ("down", "warn", "unknown", "off", "ok")


# ───────────────────────── zapis ─────────────────────────
def record_changes(conn, before, after, now=None):
    """before/after: {device_id: {"state", "why"}}. Zapisuje urządzenia, których stan się zmienił."""
    now = now or time.time()
    rows = [{"device_id": d, "ts": now, "state": v["state"], "why": (v.get("why") or "")[:300]}
            for d, v in after.items() if (before.get(d) or {}).get("state") != v["state"]]
    if rows:
        conn.execute(state_change.insert(), rows)
    return len(rows)


def record_samples(conn, results, now=None):
    """results: [(device_id, kind, ok, ms)] — zapisuje tylko te, których ostatnia próbka jest starsza niż SAMPLE_EVERY."""
    now = now or time.time()
    last = {(r.device_id, r.kind): r.ts for r in conn.execute(
        select(check_sample.c.device_id, check_sample.c.kind, func.max(check_sample.c.ts).label("ts"))
        .where(check_sample.c.ts > now - SAMPLE_EVERY).group_by(check_sample.c.device_id, check_sample.c.kind))}
    rows = [{"device_id": d, "kind": k, "ts": now, "ok": bool(ok), "ms": ms}
            for d, k, ok, ms in results if (d, k) not in last]
    if rows:
        conn.execute(check_sample.insert(), rows)
    return len(rows)


def prune(conn, now=None):
    now = now or time.time()
    conn.execute(delete(check_sample).where(check_sample.c.ts < now - KEEP_SAMPLES_DAYS * 86400))
    conn.execute(delete(state_change).where(state_change.c.ts < now - KEEP_CHANGES_DAYS * 86400))


# ───────────────────────── odczyt ─────────────────────────
def changes(conn, device_id, since):
    """Zmiany od `since` razem z ostatnią wcześniejszą (stan na początku okresu)."""
    first = conn.execute(select(state_change).where(state_change.c.device_id == device_id, state_change.c.ts < since)
                         .order_by(state_change.c.ts.desc()).limit(1)).mappings().all()
    rest = conn.execute(select(state_change).where(state_change.c.device_id == device_id, state_change.c.ts >= since)
                        .order_by(state_change.c.ts)).mappings().all()
    return list(first) + list(rest)


def down_episodes(conn, device_id, since):
    """Ile razy urządzenie przeszło w „nie działa” od `since`."""
    return conn.execute(select(func.count()).select_from(state_change).where(
        state_change.c.device_id == device_id, state_change.c.ts >= since, state_change.c.state == "down")).scalar()


def last_change(conn, device_id):
    return conn.execute(select(state_change).where(state_change.c.device_id == device_id)
                        .order_by(state_change.c.ts.desc()).limit(1)).mappings().first()


def availability(conn, device_id, hours, now=None):
    """Pasek dostępności: {"slots": [stan najgorszy w odcinku], "pct": % czasu bez awarii albo None, "down_s"}."""
    now = now or time.time()
    since = now - hours * 3600
    rows = changes(conn, device_id, since)
    if not rows:
        return {"slots": ["none"] * BAR_SLOTS, "pct": None, "down_s": 0}
    # odcinki [start, koniec, stan]
    spans = []
    for i, r in enumerate(rows):
        start = max(r["ts"], since)
        end = rows[i + 1]["ts"] if i + 1 < len(rows) else now
        if end > start:
            spans.append((start, end, r["state"]))
    known = sum(e - s for s, e, st in spans if st in ("ok", "warn", "down"))     # brak danych i wyłączone się nie liczą
    down = sum(e - s for s, e, st in spans if st == "down")
    width = (now - since) / BAR_SLOTS
    slots = []
    for k in range(BAR_SLOTS):
        a, b = since + k * width, since + (k + 1) * width
        inside = [st for s, e, st in spans if s < b and e > a]
        slots.append(next((w for w in WORST if w in inside), "none"))
    return {"slots": slots, "pct": round(100 * (known - down) / known, 2) if known else None, "down_s": down}


def response_times(conn, device_id, kind, hours, now=None):
    now = now or time.time()
    return conn.execute(select(check_sample.c.ts, check_sample.c.ok, check_sample.c.ms).where(
        check_sample.c.device_id == device_id, check_sample.c.kind == kind, check_sample.c.ts >= now - hours * 3600)
        .order_by(check_sample.c.ts)).mappings().all()


def sparkline(samples, since, until, width=600, height=60):
    """Ścieżka SVG czasu odpowiedzi; brak odpowiedzi = przerwa w linii i czerwony znacznik na dole."""
    if not samples:
        return None
    vals = [s["ms"] for s in samples if s["ok"] and s["ms"] is not None]
    top = max(vals) if vals else 1
    top = max(top * 1.15, 1)
    x = lambda ts: round((ts - since) / max(until - since, 1) * width, 1)   # noqa: E731
    parts, fails, pen = [], [], "M"
    for s in samples:
        if not s["ok"] or s["ms"] is None:
            fails.append(x(s["ts"]))
            pen = "M"
            continue
        parts.append(f"{pen}{x(s['ts'])} {round(height - s['ms'] / top * (height - 4), 1)}")
        pen = "L"
    # pojedyncze punkty (np. świeżo dopisany port) — linia z jednego punktu jest niewidoczna
    dots = [tuple(float(v) for v in p[1:].split()) for p in parts] if len(parts) < 3 else []
    return {"d": " ".join(parts), "fails": fails, "dots": dots, "max": round(top / 1.15, 1),
            "avg": round(sum(vals) / len(vals), 1) if vals else None, "w": width, "h": height}
