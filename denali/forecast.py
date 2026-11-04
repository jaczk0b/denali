"""Prognoza zapełnienia magazynów — pule TrueNAS i magazyny PBS, bez osobnej bazy metryk.

Przy każdym odczycie integracji zapisujemy zajętość (najwyżej raz na SAMPLE_EVERY). Prognoza to prosta linia
(najmniejsze kwadraty) z ostatnich WINDOW_DAYS: ile dni do FULL_AT zajętości, jeśli przyrost będzie taki jak dotąd.
Potrzeba co najmniej MIN_SPAN_DAYS danych — wcześniej to zgadywanie. Gdy zostaje mniej niż ALERT_DAYS, worker
wysyła powiadomienie (raz na REALERT_DAYS dla tego samego magazynu).
"""
import time

from sqlalchemy import delete, func, select

from .db import kv_get, kv_set
from .models import external_ref, integration, usage_sample

SAMPLE_EVERY = 3600
WINDOW_DAYS = 14
MIN_SPAN_DAYS = 2
KEEP_DAYS = 90
FULL_AT = 0.95          # ZFS i PBS zwalniają i zaczynają odmawiać zapisu przed 100%
SHOW_DAYS = 60          # „Nadchodzące” na pulpicie
ALERT_DAYS = 14
REALERT_DAYS = 7


def _from_state(s):
    """[(nazwa, used, total)] ze stanu zgłoszonego przez adapter."""
    d = s.data or {}
    if s.external_id.startswith("pool/") and d.get("size"):
        return [(f"pula {s.external_id[5:]}", float(d.get("allocated") or 0), float(d["size"]))]
    if isinstance(d.get("usage"), dict):
        return [(f"magazyn {k}", float(v.get("used") or 0), float(v["total"]))
                for k, v in d["usage"].items() if isinstance(v, dict) and v.get("total")]
    return []


def record(conn, integration_id, states, now=None):
    now = now or time.time()
    rows = [(n, u, t) for s in states for n, u, t in _from_state(s)]
    if not rows:
        return 0
    last = {r.name: r.ts for r in conn.execute(
        select(usage_sample.c.name, func.max(usage_sample.c.ts).label("ts"))
        .where(usage_sample.c.integration_id == integration_id).group_by(usage_sample.c.name))}
    new = [{"integration_id": integration_id, "name": n[:100], "ts": now, "used": u, "total": t}
           for n, u, t in rows if now - last.get(n[:100], 0) >= SAMPLE_EVERY]
    if new:
        conn.execute(usage_sample.insert(), new)
    return len(new)


def prune(conn, now=None):
    conn.execute(delete(usage_sample).where(usage_sample.c.ts < (now or time.time()) - KEEP_DAYS * 86400))


def _fit(points):
    """Nachylenie prostej (bajty na sekundę) metodą najmniejszych kwadratów."""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    var = sum((p[0] - mx) ** 2 for p in points)
    return sum((p[0] - mx) * (p[1] - my) for p in points) / var if var else 0.0


def forecasts(conn, now=None):
    """[{name, source, device_id, pct, days, per_day}] — days None, gdy nie rośnie albo za mało danych."""
    now = now or time.time()
    since = now - WINDOW_DAYS * 86400
    series = {}
    for r in conn.execute(select(usage_sample).where(usage_sample.c.ts >= since).order_by(usage_sample.c.ts)).mappings():
        series.setdefault((r["integration_id"], r["name"]), []).append(r)
    names = {r.id: r.name for r in conn.execute(select(integration.c.id, integration.c.name))}
    servers = {r.integration_id: r.device_id for r in conn.execute(
        select(external_ref.c.integration_id, external_ref.c.device_id)
        .where(external_ref.c.external_id == "server", external_ref.c.device_id.isnot(None)))}
    out = []
    for (iid, name), rows in series.items():
        last = rows[-1]
        pct = last["used"] / last["total"] if last["total"] else 0
        days = per_day = None
        if rows[-1]["ts"] - rows[0]["ts"] >= MIN_SPAN_DAYS * 86400:
            slope = _fit([(r["ts"], r["used"]) for r in rows])
            per_day = slope * 86400
            if slope > 0:
                days = max(0.0, (FULL_AT * last["total"] - last["used"]) / slope / 86400)
        out.append({"name": name, "source": names.get(iid, ""), "device_id": servers.get(iid), "pct": pct,
                    "days": days, "per_day": per_day, "key": f"{iid}:{name}"})
    out.sort(key=lambda f: (f["days"] is None, f["days"] or 0))
    return out


def alerts_due(conn, now=None):
    """Prognozy poniżej ALERT_DAYS, o których nie powiadomiono w ostatnich REALERT_DAYS."""
    now = now or time.time()
    sent = kv_get(conn, "forecast.alerted") or {}
    return [f for f in forecasts(conn, now) if f["days"] is not None and f["days"] < ALERT_DAYS
            and (f["key"] not in sent or now - sent[f["key"]] >= REALERT_DAYS * 86400)]


def mark_alerted(conn, due, now=None):
    now = now or time.time()
    kv_set(conn, "forecast.alerted", {**(kv_get(conn, "forecast.alerted") or {}), **{f["key"]: now for f in due}})


def describe(f):
    when = "już prawie pełna" if f["days"] < 1 else f"za ok. {round(f['days'])} dni"
    return f"{f['name']} ({f['source']}): zajęte {f['pct']:.0%}, zapełni się {when}"
