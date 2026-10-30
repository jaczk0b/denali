"""Incydenty = log problemów. Awaria wykryta automatycznie i wpis ręczny to ten sam rodzaj wpisu."""
import time

from sqlalchemy import case, select

from .models import device, incident, incident_device, incident_event

STATUSES = ("trwa", "potwierdzony", "zamkniety")
SEVERITIES = ("krytyczny", "ostrzezenie", "info")
VIA = {"hosting": "działa na nim", "kabel": "kabel", "zasilanie": "zasilanie", "siec": "sieć", "dane": "dane",
       "usluga": "usługa"}


class IncidentError(ValueError):
    pass


def _event(conn, incident_id, kind, text, user="", now=None):
    conn.execute(incident_event.insert().values(incident_id=incident_id, ts=now or time.time(), kind=kind, text=text,
                                                user=user))


def _get(conn, incident_id):
    row = conn.execute(select(incident).where(incident.c.id == incident_id)).mappings().first()
    if not row:
        raise IncidentError("Nie ma takiego incydentu.")
    return row


def _add_devices(conn, incident_id, device_ids, role):
    have = {r.device_id for r in conn.execute(select(incident_device.c.device_id)
                                              .where(incident_device.c.incident_id == incident_id))}
    new = [d for d in device_ids if d not in have]
    if new:
        conn.execute(incident_device.insert(), [{"incident_id": incident_id, "device_id": d, "role": role} for d in new])
    return new


def open_manual(conn, title, user, device_id=None, severity="ostrzezenie", text="", now=None):
    title = (title or "").strip()
    if not title:
        raise IncidentError("Podaj krótki opis problemu.")
    if severity not in SEVERITIES:
        raise IncidentError("Nieznana waga.")
    now = now or time.time()
    iid = conn.execute(incident.insert().values(title=title[:200], root_device_id=device_id, severity=severity,
                                                source="reczny", started_at=now)).inserted_primary_key[0]
    if device_id:
        _add_devices(conn, iid, [device_id], "przyczyna")
    _event(conn, iid, "otwarty", text or "Wpis ręczny.", user, now)
    return iid


def add_note(conn, incident_id, text, user, now=None):
    _get(conn, incident_id)
    text = (text or "").strip()
    if not text:
        raise IncidentError("Notatka jest pusta.")
    _event(conn, incident_id, "notatka", text, user, now)


def ack(conn, incident_id, user, now=None):
    """„Wiem, zajmuję się” — wycisza ponowne powiadomienia o tym incydencie."""
    inc = _get(conn, incident_id)
    if inc["status"] != "trwa":
        return
    conn.execute(incident.update().where(incident.c.id == incident_id).values(status="potwierdzony", acked_by=user))
    _event(conn, incident_id, "potwierdzony", f"{user} zajmuje się tym.", user, now)


def close(conn, incident_id, user, cause_md=None, fix_md=None, now=None, text="Zamknięty."):
    inc = _get(conn, incident_id)
    values = {}
    if cause_md is not None:
        values["cause_md"] = cause_md
    if fix_md is not None:
        values["fix_md"] = fix_md
    if inc["status"] != "zamkniety":
        now = now or time.time()
        values.update(status="zamkniety", ended_at=now)
        _event(conn, incident_id, "zamkniety", text, user, now)
    if values:
        conn.execute(incident.update().where(incident.c.id == incident_id).values(**values))


def reopen(conn, incident_id, user, now=None):
    if _get(conn, incident_id)["status"] == "zamkniety":
        conn.execute(incident.update().where(incident.c.id == incident_id).values(status="trwa", ended_at=None))
        _event(conn, incident_id, "wznowiony", "Wznowiony.", user, now)


# ───────────────────────── incydenty z analizy przyczyny ─────────────────────────
def _open_auto(conn, root_device_id):
    return conn.execute(select(incident).where(incident.c.root_device_id == root_device_id,
                                               incident.c.source == "auto", incident.c.status != "zamkniety")
                        ).mappings().first()


def record_finding(conn, graph, finding, now=None):
    """Zapisuje wynik analizy (rca.Finding) jako incydent. Ta sama przyczyna = ten sam otwarty incydent.
    Zwraca (id, czy_nowy)."""
    now = now or time.time()
    names = graph.name
    existing = _open_auto(conn, finding.cause)
    if existing:
        iid = existing["id"]
        new = _add_devices(conn, iid, finding.effects, "skutek")
        if new:
            _event(conn, iid, "skutek", "Przestało też działać: " + ", ".join(names(d) for d in new) + ".", now=now)
        _add_devices(conn, iid, finding.degraded, "gorzej")
        return iid, False

    crit = {r.id: r.criticality for r in conn.execute(
        select(device.c.id, device.c.criticality).where(device.c.id.in_([finding.cause, *finding.effects])))}
    severity = "krytyczny" if 1 in crit.values() else "ostrzezenie"
    iid = conn.execute(incident.insert().values(title=f"{names(finding.cause)} nie działa", source="auto",
                                                root_device_id=finding.cause, severity=severity, started_at=now)
                       ).inserted_primary_key[0]
    _add_devices(conn, iid, [finding.cause], "przyczyna")
    _add_devices(conn, iid, finding.effects, "skutek")
    _add_devices(conn, iid, finding.degraded, "gorzej")
    _event(conn, iid, "otwarty", f"Wykryto: {names(finding.cause)} nie działa.", now=now)
    for d, via in finding.upstream_ok:
        _event(conn, iid, "dowod", f"{names(d)} działa ({VIA.get(via, via)}), więc przyczyna nie jest wyżej.", now=now)
    for d, via in finding.upstream_unknown:
        _event(conn, iid, "dowod", f"Brak danych o {names(d)} ({VIA.get(via, via)}) — sprawdź też to.", now=now)
    if finding.effects:
        _event(conn, iid, "skutek", "Przez to nie działa: " + ", ".join(names(d) for d in finding.effects) + ".",
               now=now)
    if finding.degraded:
        _event(conn, iid, "skutek", "Działa gorzej: " + ", ".join(names(d) for d in finding.degraded) + ".", now=now)
    return iid, True


def recover(conn, device_id, now=None):
    """Przyczyna znowu działa — zamyka jej automatyczny incydent. Opis przyczyny i naprawy można dopisać później."""
    inc = _open_auto(conn, device_id)
    if inc:
        close(conn, inc["id"], "system", now=now, text="Przyczyna znowu działa — zamknięty automatycznie.")
    return inc["id"] if inc else None


# ───────────────────────── odczyt ─────────────────────────
def get_incident(conn, incident_id):
    out = dict(_get(conn, incident_id))
    out["devices"] = conn.execute(select(device.c.id, device.c.name, incident_device.c.role)
                                  .join(incident_device, incident_device.c.device_id == device.c.id)
                                  .where(incident_device.c.incident_id == incident_id)
                                  .order_by(case({"przyczyna": 0, "skutek": 1}, value=incident_device.c.role, else_=2),
                                            device.c.name)).mappings().all()
    out["events"] = conn.execute(select(incident_event).where(incident_event.c.incident_id == incident_id)
                                 .order_by(incident_event.c.ts, incident_event.c.id)).mappings().all()
    return out


def list_incidents(conn, open_only=False, device_id=None, limit=100):
    q = select(incident).order_by(incident.c.started_at.desc()).limit(limit)
    if open_only:
        q = q.where(incident.c.status != "zamkniety")
    if device_id is not None:
        q = q.where(incident.c.id.in_(select(incident_device.c.incident_id)
                                      .where(incident_device.c.device_id == device_id)))
    return conn.execute(q).mappings().all()
