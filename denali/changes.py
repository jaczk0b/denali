"""„Co się zmieniło przed awarią” — dowody dopisywane do nowego incydentu.

Awaria rzadko bierze się znikąd. Gdy analiza przyczyny otwiera incydent, zbieramy z ostatnich WINDOW sekund zmiany
przy przyczynie i przy wszystkim, od czego ona zależy (przechodnio, także miękko):
- akcje z panelu (restart, aktualizacja, snapshot, deploy) — z wynikiem i tym, kto zlecił,
- nowa kopia konfiguracji z inną sumą kontrolną niż poprzednia (ktoś zmienił ustawienia routera),
- zmiany stanu (np. host od kwadransa „uwaga”, chwilowa przerwa w pingu),
- koniec planowanej przerwy (restart się skończył, ale coś nie wstało),
- edycja dokumentacji urządzenia (zmieniony adres, rodzic, zależności).
Najświeższe na górze, najwyżej LIMIT — w przebiegu incydentu i w powiadomieniu.
"""
import time

from sqlalchemy import select

from .models import action_run, config_backup, device, maintenance, state_change

WINDOW = 2 * 3600
LIMIT = 6
STATE_WORD = {"ok": "działa", "warn": "uwaga", "down": "nie działa", "off": "wyłączone", "unknown": "brak danych"}


def _ago(seconds):
    m = max(0, round(seconds / 60))
    if m < 1:
        return "chwilę wcześniej"
    if m < 60:
        return f"{m} min wcześniej"
    h, m = divmod(m, 60)
    return f"{h} h {m} min wcześniej" if m else f"{h} h wcześniej"


def before(conn, graph, cause, now=None, window=WINDOW):
    """[(ts, tekst)] — zmiany przy przyczynie i tym, od czego zależy, od najświeższej."""
    now = now or time.time()
    since = now - window
    around = {cause} | graph.ancestors(cause, hard_only=False)
    name = graph.name
    out = []
    for r in conn.execute(select(action_run).where(action_run.c.device_id.in_(around),
                                                   action_run.c.created_at >= since)).mappings():
        status = {"ok": "wykonana", "blad": "błąd", "trwa": "w trakcie"}.get(r["status"], r["status"])
        ts = r["finished_at"] or r["started_at"] or r["created_at"]
        out.append((ts, f"{_ago(now - ts)}: {r['label']} na {name(r['device_id'])} ({r['requested_by']}) — {status}"))
    for d in around:
        backups = conn.execute(select(config_backup.c.taken_at, config_backup.c.sha256).where(
            config_backup.c.device_id == d).order_by(config_backup.c.taken_at.desc()).limit(2)).all()
        if len(backups) == 2 and backups[0].taken_at >= since and backups[0].sha256 != backups[1].sha256:
            out.append((backups[0].taken_at,
                        f"{_ago(now - backups[0].taken_at)}: konfiguracja {name(d)} jest inna niż w poprzedniej kopii"))
    for r in conn.execute(select(state_change).where(state_change.c.device_id.in_(around - {cause}),
                                                     state_change.c.ts >= since, state_change.c.state != "ok")
                          .order_by(state_change.c.ts.desc()).limit(10)).mappings():
        why = f" ({r['why']})" if r["why"] else ""
        out.append((r["ts"], f"{_ago(now - r['ts'])}: {name(r['device_id'])} — {STATE_WORD.get(r['state'], r['state'])}{why}"))
    for r in conn.execute(select(maintenance).where(maintenance.c.device_id.in_(around),
                                                    maintenance.c.ends_at >= since, maintenance.c.ends_at <= now)
                          ).mappings():
        reason = f" ({r['reason']})" if r["reason"] else ""
        out.append((r["ends_at"], f"{_ago(now - r['ends_at'])}: skończyła się planowana przerwa na {name(r['device_id'])}{reason}"))
    for r in conn.execute(select(device.c.id, device.c.updated_at, device.c.created_at).where(
            device.c.id.in_(around), device.c.updated_at >= since)).mappings():
        if r["updated_at"] - r["created_at"] > 5:   # świeżo dodane urządzenie to nie zmiana
            out.append((r["updated_at"], f"{_ago(now - r['updated_at'])}: zmieniono dokumentację {name(r['id'])}"))
    out.sort(key=lambda x: -x[0])
    return out[:LIMIT]
