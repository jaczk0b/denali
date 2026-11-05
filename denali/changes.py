"""„Co się zmieniło przed awarią” — dowody dopisywane do nowego incydentu.

Awaria rzadko bierze się znikąd. Gdy analiza przyczyny otwiera incydent, zbieramy z ostatnich WINDOW sekund zmiany
przy przyczynie i przy wszystkim, od czego ona zależy (przechodnio, także miękko):
- akcje z panelu (restart, aktualizacja, snapshot, deploy) — z wynikiem i tym, kto zlecił,
- nowa kopia konfiguracji z inną sumą kontrolną niż poprzednia (ktoś zmienił ustawienia routera),
- zmiany stanu (np. host od kwadransa „uwaga”, chwilowa przerwa w pingu),
- koniec planowanej przerwy (restart się skończył, ale coś nie wstało),
- edycja dokumentacji urządzenia (zmieniony adres, rodzic, zależności),
- zmiany wykryte w danych ze źródeł, także zrobione poza panelem (detect niżej): nowy obraz, nowy lub usunięty
  kontener, restart systemu, zmiana adresu, instalacja pakietów.
Najświeższe na górze, najwyżej LIMIT — w przebiegu incydentu i w powiadomieniu.
"""
import time

from sqlalchemy import delete, select

from .models import (action_run, change_event, config_backup, device, external_ref, incident, maintenance,
                     state_change)

WINDOW = 24 * 3600
LIMIT = 8
KEEP_DAYS = 90
RESTART_SLACK = 300         # czas działania zmalał o tyle: to restart, a nie niedokładność źródła
KIND_LABEL = {"obraz": "obraz", "kontener": "kontener", "restart": "restart", "adres": "adres", "stan": "stan",
              "pakiety": "pakiety", "akcja": "akcja", "incydent": "incydent", "hlc": "z HLC"}
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
    for r in conn.execute(select(change_event).where(change_event.c.device_id.in_(around), change_event.c.ts >= since)
                          ).mappings():
        out.append((r["ts"], f"{_ago(now - r['ts'])}: {name(r['device_id'])}: {r['text']}"))
    out.sort(key=lambda x: -x[0])
    return out[:LIMIT]


# ───────────────────────── wykrywanie zmian w danych ze źródeł ─────────────────────────
def _containers(rows):
    return {str(c.get("name")): c for c in rows if isinstance(c, dict) and c.get("name")}


def diff(old, new):
    """[(rodzaj, tekst)] — co się zmieniło między dwoma odczytami tego samego obiektu. Puste pole = nie wiadomo, więc
    bez wniosków: agent sprzed 1.4 albo pierwszy odczyt nie tworzą fałszywych zmian."""
    out = []
    if not old:
        return out
    if old.get("boot_id") and new.get("boot_id") and old["boot_id"] != new["boot_id"]:
        out.append(("restart", "restart systemu"))
    a, b = old.get("uptime"), new.get("uptime")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0 and b + RESTART_SLACK < a:
        out.append(("restart", "restart (czas działania od nowa)"))
    a, b = old.get("status"), new.get("status")
    if a and b and a != b:
        out.append(("stan", f"{a} → {b}"))
    a, b = old.get("ips"), new.get("ips")
    if a and b and sorted(a) != sorted(b):
        out.append(("adres", f"adresy: {', '.join(sorted(a))} → {', '.join(sorted(b))}"))
    a, b = old.get("dpkg_changed"), new.get("dpkg_changed")
    if a and b and a != b:
        out.append(("pakiety", "zmieniły się zainstalowane pakiety"))
    if isinstance(old.get("containers"), list) and isinstance(new.get("containers"), list):
        was, now_ = _containers(old["containers"]), _containers(new["containers"])
        for n in sorted(set(now_) - set(was)):
            out.append(("kontener", f"nowy kontener {n} ({now_[n].get('image', '?')})"))
        for n in sorted(set(was) - set(now_)):
            out.append(("kontener", f"usunięty kontener {n}"))
        for n in sorted(set(was) & set(now_)):
            x, y = was[n], now_[n]
            if x.get("image_id") and y.get("image_id") and x["image_id"] != y["image_id"]:
                out.append(("obraz", f"{n}: nowy obraz {y.get('image', '')}".rstrip()))
            r1, r2 = x.get("restarts"), y.get("restarts")
            if isinstance(r1, int) and isinstance(r2, int) and r2 > r1:
                out.append(("restart", f"{n}: restart kontenera (razem {r2})"))
    if isinstance(old.get("images"), list) and isinstance(new.get("images"), list):
        was = {x.get("service"): x.get("image") for x in old["images"] if isinstance(x, dict)}
        for x in new["images"]:
            if isinstance(x, dict) and x.get("service") in was and was[x["service"]] != x.get("image"):
                out.append(("obraz", f"{x['service']}: {was[x['service']]} → {x.get('image')}"))
    return out


def record(conn, integration_id, external_id, old, new, now=None):
    """Zapisuje zmiany obiektu przy jego urządzeniu (obiekt niezatwierdzony: bez zapisu, nie ma go gdzie przypiąć)."""
    found = diff(old or {}, new or {})
    if not found:
        return 0
    device_id = conn.execute(select(external_ref.c.device_id).where(
        external_ref.c.integration_id == integration_id, external_ref.c.external_id == external_id,
        external_ref.c.status == "zatwierdzony")).scalar()
    if not device_id:
        return 0
    conn.execute(change_event.insert(), [{"device_id": device_id, "ts": now or time.time(), "kind": k, "text": t[:300]}
                                         for k, t in found])
    return len(found)


def recent(conn, device_id, since, limit=20):
    return conn.execute(select(change_event).where(change_event.c.device_id == device_id, change_event.c.ts >= since)
                        .order_by(change_event.c.ts.desc()).limit(limit)).mappings().all()


def timeline(conn, since, device_id=None, limit=300):
    """Oś czasu: zmiany, akcje, stany i incydenty w jednej liście, od najnowszych."""
    names = {r.id: r.name for r in conn.execute(select(device.c.id, device.c.name))}
    out = []

    def add(ts, kind, did, text):
        if device_id is None or did == device_id:
            out.append({"ts": ts, "kind": kind, "label": KIND_LABEL.get(kind, kind), "device_id": did,
                        "device": names.get(did, ""), "text": text})
    for r in conn.execute(select(change_event).where(change_event.c.ts >= since)).mappings():
        add(r["ts"], r["kind"], r["device_id"], r["text"])
    for r in conn.execute(select(state_change).where(state_change.c.ts >= since)).mappings():
        word = STATE_WORD.get(r["state"], r["state"])
        add(r["ts"], "stan", r["device_id"], f"{word}{' (' + r['why'] + ')' if r['why'] else ''}")
    for r in conn.execute(select(action_run).where(action_run.c.created_at >= since)).mappings():
        add(r["created_at"], "akcja", r["device_id"], f"{r['label']} ({r['requested_by']})")
    for r in conn.execute(select(incident).where(incident.c.started_at >= since)).mappings():
        add(r["started_at"], "incydent", r["root_device_id"], r["title"])
    out.sort(key=lambda x: -x["ts"])
    return out[:limit]


def prune(conn, now=None):
    conn.execute(delete(change_event).where(change_event.c.ts < (now or time.time()) - KEEP_DAYS * 86400))
