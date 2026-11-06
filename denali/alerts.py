"""Alerty: konkretne problemy, które nie są awarią urządzenia, ale nie mogą przejść bez słowa.

Incydent mówi „urządzenie nie działa”. Alert mówi „działa, ale pula jest zdegradowana / dysk się kończy / kopia jest
stara”. Stan „uwaga” sam niczego nie wysyła, bo uwagą jest też np. lista pakietów sprzed tygodnia. Na powiadomienie
zasługują tylko rzeczy z RULES.

Źródła zgłaszają surowe fakty w `data["alarms"]` stanu obiektu: {"rule", "text", "key"?, "severity"?, "pct"?}. Tu
dochodzą progi z profilu, wyciszenia i planowane przerwy. Dzięki temu adaptery nie znają ustawień.

- powiadomienie po ALERT_WAIT (chwilowy skok nie wysyła wiadomości),
- „rozwiązane” po RESOLVE_AFTER ciszy (migotanie wokół progu nie robi serii),
- gdy źródło milczy (agent nie raportuje), alert zostaje: brak danych to nie naprawa,
- gdy urządzenie leży albo ma planowaną przerwę, alert czeka: mówi o tym incydent.
"""
import time

from sqlalchemy import delete, select

from . import maintenance, profiles, terms, uplink
from .models import alert, check_state, device, external_ref, observed_state

ALERT_WAIT = 300
RESOLVE_AFTER = 600
KEEP_DAYS = 90
CERT_WARN_DAYS = 14
CERT_CRIT_DAYS = 5
INTERNET_AFTER = 300
WARN, CRIT = "ostrzezenie", "krytyczny"
SILENCE_DAYS = (1, 7, 30)

# reguła → (etykieta, domyślna waga)
RULES = {
    "disk": ("Zajętość dysku", WARN),
    "inode": ("Zajętość i-węzłów", WARN),
    "memory": ("Pamięć RAM", WARN),
    "swap": ("Swap", WARN),
    "storage": ("Magazyn się zapełnia", WARN),
    "pool_unhealthy": ("Pula ZFS niezdrowa", CRIT),
    "smart_failed": ("Dysk: test SMART nieudany", CRIT),
    "disk_hot": ("Dysk za gorący", WARN),
    "truenas_alert": ("Alert TrueNAS", WARN),
    "backup_task": ("Snapshoty lub replikacja", WARN),
    "backup_stale": ("Stara kopia zapasowa", WARN),
    "nfs": ("NFS niezamontowany albo nie odpowiada", CRIT),
    "nfs_hidden": ("Dane pod montażem NFS", WARN),
    "nfs_slow": ("NFS wolny", WARN),
    "stack_unhealthy": ("Stack Docker niezdrowy", CRIT),
    "container_unhealthy": ("Kontener niezdrowy", CRIT),
    "container_restarting": ("Kontener się restartuje", WARN),
    "container_exited": ("Kontener zatrzymany z błędem", WARN),
    "container_layer": ("Warstwa zapisywalna kontenera", WARN),
    "core_dumps": ("Pliki core w kontenerze", WARN),
    "systemd": ("Usługa systemd w stanie failed", WARN),
    "cert": ("Certyfikat wygasa", WARN),
    "external": ("Działa w LAN, nie działa z internetu", WARN),
    "term": ("Termin przy urządzeniu", WARN),
    "cluster_quorum": ("Klaster Proxmox bez kworum", CRIT),
    "internet_down": ("Brak internetu w domu", CRIT),
    "widget": ("Próg statystyki z pulpitu", WARN),
}


def _text(rule, text):
    return (text or RULES.get(rule, (rule,))[0])[:500]


def _percent(conn, device_id, item, cache):
    """Waga alertu procentowego według progów z profilu; None = poniżej progu."""
    if device_id not in cache:
        cache[device_id] = (profiles.value(conn, device_id, "disk_warn"), profiles.value(conn, device_id, "disk_crit"))
    warn, crit = cache[device_id]
    pct = item.get("pct")
    if not isinstance(pct, (int, float)) or pct < warn:
        return None
    return CRIT if pct >= crit else WARN


def collect(conn, now=None):
    """(obecne problemy, źródła bez świeżych danych). Problem: dict z kluczami kolumn tabeli alert."""
    now = now or time.time()
    found, silent, cache, monitored = {}, set(), {}, {}

    def watched(device_id):
        if device_id not in monitored:
            monitored[device_id] = profiles.monitored(conn, device_id)
        return monitored[device_id]

    def add(rule, device_id, key, severity, text, integration_id=None, external_id=None):
        fp = f"{rule}:{device_id or '-'}:{key}"[:400]
        found.setdefault(fp, {"fingerprint": fp, "device_id": device_id, "rule": rule, "severity": severity,
                              "message": _text(rule, text), "integration_id": integration_id,
                              "external_id": external_id})

    rows = conn.execute(select(external_ref.c.device_id, observed_state.c.integration_id, observed_state.c.external_id,
                               observed_state.c.state, observed_state.c.data)
                        .join(observed_state, (observed_state.c.integration_id == external_ref.c.integration_id)
                              & (observed_state.c.external_id == external_ref.c.external_id))
                        .where(external_ref.c.status == "zatwierdzony", external_ref.c.device_id.isnot(None))).all()
    for r in rows:
        if r.state == "unknown":
            silent.add((r.integration_id, r.external_id))
            continue
        items = (r.data or {}).get("alarms")
        if not isinstance(items, list) or not watched(r.device_id):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("rule") not in RULES:
                continue
            rule = item["rule"]
            severity = _percent(conn, r.device_id, item, cache) if rule in ("disk", "inode") \
                else item.get("severity") or RULES[rule][1]
            if severity:
                add(rule, r.device_id, item.get("key") or r.external_id, severity, item.get("text"),
                    r.integration_id, r.external_id)

    for c in conn.execute(select(check_state)).mappings():
        if not watched(c["device_id"]):
            continue
        if c["cert_expires_at"] and c["ok"]:
            left = (c["cert_expires_at"] - now) / 86400
            if left < CERT_WARN_DAYS:
                host = c["target"].split("/")[2] if "//" in c["target"] else c["target"]
                add("cert", c["device_id"], host, CRIT if left < CERT_CRIT_DAYS else WARN,
                    f"Certyfikat {host} wygasa za {max(0, round(left))} dni")
        if c["kind"] == "internet" and not c["ok"] and c["first_fail_at"] and now - c["first_fail_at"] >= INTERNET_AFTER:
            add("external", c["device_id"], c["target"], WARN, f"{c['target']}: {c['summary']}")

    for t in terms.due(conn, now):
        if watched(t["device_id"]):
            add("term", t["device_id"], t["id"], t["severity"], t["text"])

    if uplink.is_down(conn):
        router = conn.execute(select(device.c.id).where(device.c.kind.in_(("modem", "router", "firewall")))
                              .order_by(device.c.kind.desc()).limit(1)).scalar()
        add("internet_down", router, "uplink", CRIT, "Brak internetu w domu: 1.1.1.1, 8.8.8.8 i 9.9.9.9 nie odpowiadają")
    from . import deskview                  # deskview czyta alerty do panelu bocznego, więc import dopiero tutaj
    for b in deskview.breaches(conn, now):
        if b["device_id"] is None or watched(b["device_id"]):
            add("widget", b["device_id"], b["key"], b["severity"], b["text"], b["integration_id"], b["external_id"])
    return list(found.values()), silent


def evaluate(conn, now=None):
    """Zapisuje obecne problemy i zamyka te, których już nie ma. Zwraca liczbę nowych alertów."""
    now = now or time.time()
    found, silent = collect(conn, now)
    current = {r["fingerprint"]: r for r in conn.execute(select(alert).where(alert.c.resolved_at.is_(None))).mappings()}
    created = 0
    for f in found:
        row = current.pop(f["fingerprint"], None)
        if row:
            conn.execute(alert.update().where(alert.c.id == row["id"]).values(
                last_seen=now, message=f["message"], severity=f["severity"]))
        else:
            conn.execute(alert.insert().values(**f, first_seen=now, last_seen=now))
            created += 1
    for row in current.values():
        if (row["integration_id"], row["external_id"]) in silent or now - row["last_seen"] < RESOLVE_AFTER:
            continue
        conn.execute(alert.update().where(alert.c.id == row["id"]).values(resolved_at=now))
    return created


def active(conn, device_id=None, now=None):
    """Otwarte alerty, od najpoważniejszych. Wyciszone też są na liście (z datą) — nie znikają z widoku."""
    q = select(alert, device.c.name.label("device")).join(device, device.c.id == alert.c.device_id, isouter=True) \
        .where(alert.c.resolved_at.is_(None))
    if device_id is not None:
        q = q.where(alert.c.device_id == device_id)
    rows = [dict(r) for r in conn.execute(q).mappings()]
    now = now or time.time()
    for r in rows:
        r["silenced"] = bool(r["silenced_until"] and r["silenced_until"] > now)
        r["label"] = RULES.get(r["rule"], (r["rule"],))[0]
    rows.sort(key=lambda r: (r["silenced"], r["severity"] != CRIT, r["first_seen"]))
    return rows


def recent(conn, limit=50):
    rows = conn.execute(select(alert, device.c.name.label("device"))
                        .join(device, device.c.id == alert.c.device_id, isouter=True)
                        .where(alert.c.resolved_at.isnot(None)).order_by(alert.c.resolved_at.desc()).limit(limit)
                        ).mappings().all()
    return [{**r, "label": RULES.get(r["rule"], (r["rule"],))[0]} for r in rows]


def silence(conn, alert_id, days, now=None):
    if days not in SILENCE_DAYS:
        raise ValueError("Nieznany czas wyciszenia.")
    return conn.execute(alert.update().where(alert.c.id == alert_id, alert.c.resolved_at.is_(None))
                        .values(silenced_until=(now or time.time()) + days * 86400)).rowcount


def unsilence(conn, alert_id):
    return conn.execute(alert.update().where(alert.c.id == alert_id).values(silenced_until=None)).rowcount


def to_notify(conn, live_states, now=None):
    """(do wysłania, do oznaczenia bez wysyłki). Czekają: świeże, wyciszone i przy leżącym urządzeniu."""
    now = now or time.time()
    breaks = {m["device_id"] for m in maintenance.active(conn).values()}
    send, skip = [], []
    for r in active(conn, now=now):
        if r["notified_at"] or r["silenced"] or now - r["first_seen"] < ALERT_WAIT:
            continue
        d = r["device_id"]
        if r["rule"] == "internet_down":
            continue        # bez łącza i tak nie wyjdzie; podsumowanie po powrocie (resolved_pending)
        if d is not None:
            if d in breaks or (live_states.get(d) or {}).get("state") in ("down", "off"):
                continue
            if not profiles.value(conn, d, "notify") or not profiles.value(conn, d, "notify_warn"):
                skip.append(r)
                continue
        send.append(r)
    return send, skip


def mark_notified(conn, ids, now=None, silently=False):
    if ids:
        ts = now or time.time()
        values = {"notified_at": ts}
        if silently:
            values["resolved_notified_at"] = ts
        conn.execute(alert.update().where(alert.c.id.in_(ids)).values(**values))


def resolved_pending(conn):
    """Rozwiązane, o których poszło powiadomienie, i przerwy w internecie: o nich mówimy dopiero po powrocie łącza."""
    rows = conn.execute(select(alert, device.c.name.label("device"))
                        .join(device, device.c.id == alert.c.device_id, isouter=True)
                        .where(alert.c.resolved_at.isnot(None), alert.c.resolved_notified_at.is_(None))).mappings().all()
    return [r for r in rows if r["notified_at"] or r["rule"] == "internet_down"]


def mark_resolved_notified(conn, ids, now=None):
    if ids:
        conn.execute(alert.update().where(alert.c.id.in_(ids)).values(resolved_notified_at=now or time.time()))


def prune(conn, now=None):
    conn.execute(delete(alert).where(alert.c.resolved_at < (now or time.time()) - KEEP_DAYS * 86400))
