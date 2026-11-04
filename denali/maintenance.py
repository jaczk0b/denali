"""Planowane przerwy: „restartuję pve na 30 minut — nie alarmuj”.

W czasie przerwy urządzenie i wszystko, co od niego twardo zależy (graf z rca.py), zamiast „nie działa” ma stan
„wyłączone” z powodem przerwy — nie otwiera incydentu i nie wysyła powiadomień. Incydent otwarty przed przerwą zostaje.
Przerwa kończy się sama o czasie albo przyciskiem „Zakończ teraz”.
"""
import time

from sqlalchemy import select

from .models import device, maintenance

MIN_MINUTES, MAX_MINUTES = 5, 24 * 60
CHOICES = (15, 30, 60, 120, 240)


class MaintenanceError(ValueError):
    pass


def start(conn, device_id, minutes, reason="", user="", now=None):
    now = now or time.time()
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise MaintenanceError("Podaj, ile minut ma trwać przerwa.") from None
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise MaintenanceError(f"Przerwa: od {MIN_MINUTES} minut do {MAX_MINUTES // 60} godzin.")
    return conn.execute(maintenance.insert().values(
        device_id=device_id, starts_at=now, ends_at=now + minutes * 60, reason=(reason or "").strip()[:200],
        created_by=user)).inserted_primary_key[0]


def end(conn, maintenance_id, device_id, now=None):
    now = now or time.time()
    conn.execute(maintenance.update().where(maintenance.c.id == maintenance_id, maintenance.c.device_id == device_id,
                                            maintenance.c.ends_at > now).values(ends_at=now))


def active(conn, now=None):
    """{device_id: przerwa kończąca się najpóźniej} — trwające teraz."""
    now = now or time.time()
    out = {}
    for r in conn.execute(select(maintenance).where(maintenance.c.starts_at <= now, maintenance.c.ends_at > now)
                          .order_by(maintenance.c.ends_at)).mappings():
        out[r["device_id"]] = r
    return out


def covered(conn, graph, now=None):
    """{device_id: przerwa} dla urządzeń w przerwie i wszystkiego, co od nich twardo zależy."""
    out = {}
    for root, m in active(conn, now).items():
        hard, _ = graph.impact(root)
        for d in (root, *hard):
            if d not in out or out[d]["ends_at"] < m["ends_at"]:
                out[d] = m
    return out


def describe(m, graph=None):
    end_hm = time.strftime("%H:%M", time.localtime(m["ends_at"]))
    who = f" ({graph.name(m['device_id'])})" if graph else ""
    return f"Planowana przerwa{who} do {end_hm}" + (f": {m['reason']}" if m["reason"] else "")


def host_plan(conn, graph, host_id):
    """Bezpieczny restart hosta: maszyny na nim w kolejności wyłączania (najpierw te, które zależą od innych
    maszyn tego hosta — np. VM z Dockerem przed NAS-em, z którego czyta dane). Uruchamianie — odwrotnie."""
    rows = conn.execute(select(device.c.id, device.c.name, device.c.kind).where(device.c.parent_id == host_id)
                        .order_by(device.c.name)).mappings().all()
    guests = {r["id"]: dict(r) for r in rows if r["kind"] in ("vm", "lxc", "nas")}
    for g in guests.values():
        g["needs"] = sorted(graph.name(a) for a in graph.ancestors(g["id"], hard_only=False) & set(guests))
        g["apps"] = sorted(graph.name(c) for c in graph.impact(g["id"])[0] if c not in guests)
    order = sorted(guests.values(), key=lambda g: (-len(g["needs"]), g["name"].lower()))
    return {"shutdown": order, "start": list(reversed(order))}
