"""Stan urządzeń na żywo i automatyczne incydenty.

Stan urządzenia łączy wszystkie źródła:
- integracje (np. Proxmox): ok | warn | down | off | unknown,
- sprawdzenia z LAN (ping, HTTP): „nie działa” dopiero, gdy nie odpowiada dłużej niż próg z profilu
  (down_after_s) — krótka czkawka nie robi alarmu,
- sprawdzenie z internetu i DNS: najwyżej „uwaga” — w domu aplikacja działa, więc nie jest przyczyną awarii;
  zwykle winny jest Pangolin, DNS albo certyfikat.

Pierwszeństwo: off (wyłączone celowo — wtedy milczący ping nie jest awarią) → down → warn → ok → unknown.
Worker co minutę liczy stany, zapisuje je (strony panelu tylko je czytają), puszcza analizę przyczyny
i otwiera albo zamyka incydenty.
"""
import hashlib
import json
import time

from sqlalchemy import func, select

from . import changes, checks, history, incidents, inventory as inv, maintenance, profiles, uplink
from .db import kv_get, kv_set
from .models import check_state, device, external_ref, incident, incident_event, observed_state

STATES_KEY = "live.states"
WARN_ONLY = ("internet", "dns")     # urządzenie działa — zawodzi droga do niego, więc najwyżej „uwaga”
STALE_AFTER = 300           # stan starszy niż 5 min = worker nie działa → nie pokazujemy go jako aktualnego
# Migotanie: tyle przejść w „nie działa” w oknie = niestabilne. Wtedy incydent zamyka się dopiero po FLAP_CALM
# spokoju — jedno powiadomienie i jedno „rozwiązane” zamiast serii.
FLAP_WINDOW = 1800
FLAP_COUNT = 3
FLAP_CALM = 600


def compute(conn, now=None):
    """{device_id: {"state", "why"}} dla monitorowanych urządzeń."""
    now = now or time.time()
    out = {}
    from_sources = {}
    q = (select(external_ref.c.device_id, observed_state.c.state, observed_state.c.summary)
         .join(observed_state, (observed_state.c.integration_id == external_ref.c.integration_id)
               & (observed_state.c.external_id == external_ref.c.external_id))
         .where(external_ref.c.status == "zatwierdzony", external_ref.c.device_id.isnot(None)))
    for r in conn.execute(q):
        from_sources.setdefault(r.device_id, []).append((r.state, r.summary))
    from_checks = {}
    for c in conn.execute(select(check_state)).mappings():
        from_checks.setdefault(c["device_id"], []).append(c)
    offline = uplink.is_down(conn)
    for d, kind in conn.execute(select(device.c.id, device.c.kind)).all():
        if not profiles.value(conn, d, "monitor"):
            continue
        down_after = profiles.value(conn, d, "down_after_s")
        states = list(from_sources.get(d, []))
        pending_until = None    # najwcześniejsza chwila, w której niedziałające sprawdzenie zostanie uznane za awarię
        for c in from_checks.get(d, []):
            if offline and not c["ok"] and (kind == "vps" or checks.needs_internet(c["kind"], c["target"])):
                states.append(("unknown", "brak internetu w domu — nie da się sprawdzić"))
            elif c["ok"]:
                soon = c["cert_expires_at"] and c["cert_expires_at"] - now < 14 * 86400
                states.append(("warn" if soon else "ok", c["summary"]))
            elif not c["first_fail_at"]:
                states.append(("unknown", c["summary"]))
            elif now - c["first_fail_at"] >= down_after:
                states.append(("warn" if c["kind"] in WARN_ONLY else "down", c["summary"]))
            else:
                until = c["first_fail_at"] + down_after
                pending_until = min(pending_until or until, until)
                states.append(("unknown", f"{c['summary']} (jeśli nie wróci do "
                                          f"{time.strftime('%H:%M', time.localtime(until))}, uznam to za awarię)"))
        if not states:
            continue
        kinds = [s for s, _ in states]
        for wanted in ("off", "down", "warn", "ok", "unknown"):
            if wanted in kinds:
                out[d] = {"state": wanted, "why": "; ".join(w for s, w in states if s == wanted)[:300]}
                if wanted == "unknown" and pending_until:
                    out[d]["pending_until"] = pending_until
                break
    return out


def saved(conn, now=None):
    """Stany zapisane przez workera: {device_id: {"state", "why"}} albo {} gdy nieaktualne."""
    now = now or time.time()
    data = kv_get(conn, STATES_KEY) or {}
    if not data or now - data.get("at", 0) > STALE_AFTER:
        return {}
    return {int(k): v for k, v in data.get("devices", {}).items()}


def version(conn, now=None):
    """Krótki odcisk wszystkiego, co pokazują strony „na żywo” (pulpit, listy, karta incydentu).
    Zmienia się → przeglądarka pobiera stronę od nowa (SSE, web/live_routes.py)."""
    now = now or time.time()
    parts = {
        "stany": saved(conn, now),
        "incydenty": [list(r) for r in conn.execute(
            select(incident.c.id, incident.c.status, incident.c.severity, incident.c.title, incident.c.ended_at)
            .where(incident.c.status != "zamkniety").order_by(incident.c.id))],
        # koniec wyciszenia ma rozwinąć baner bez przeładowania
        "wyciszone": [r.id for r in conn.execute(select(incident.c.id).where(incident.c.muted_until > now).order_by(incident.c.id))],
        "zdarzenie": conn.execute(select(func.max(incident_event.c.id))).scalar(),
        "urzadzenia": list(conn.execute(select(func.count(), func.max(device.c.updated_at))).one()),
        "kolejka": conn.execute(select(func.count()).select_from(external_ref)
                                .where(external_ref.c.status == "nowy")).scalar(),
        "dzien": time.strftime("%Y-%m-%d", time.localtime(now)),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def evaluate(conn, now=None):
    """Liczy stany, zapisuje je i aktualizuje incydenty. Zwraca listę ustaleń analizy przyczyny."""
    now = now or time.time()
    before = {int(k): v for k, v in ((kv_get(conn, STATES_KEY) or {}).get("devices") or {}).items()}
    live = compute(conn, now)
    g = inv.load_graph(conn)
    planned = maintenance.covered(conn, g, now)
    for d, m in planned.items():
        if d in live and live[d]["state"] in ("down", "unknown"):
            live[d] = {"state": "off", "why": maintenance.describe(m, g if m["device_id"] != d else None)}
    history.record_changes(conn, before, live, now)
    kv_set(conn, STATES_KEY, {"at": now, "devices": {str(k): v for k, v in live.items()}})
    findings = g.analyze({d: v["state"] for d, v in live.items()})
    causes = {f.cause for f in findings}
    effect_of = {e: f.cause for f in findings for e in f.effects}
    for f in findings:
        iid, new = incidents.record_finding(conn, g, f, now, why=live.get(f.cause, {}).get("why", ""))
        if new:
            for ts, text in reversed(changes.before(conn, g, f.cause, now)):
                incidents.add_event(conn, iid, "zmiana", text, now=now)
    open_auto = conn.execute(select(incident.c.id, incident.c.root_device_id).where(
        incident.c.source == "auto", incident.c.status != "zamkniety")).all()
    for inc in open_auto:
        root = inc.root_device_id
        if root in causes:
            continue
        if root in effect_of:
            # przyczyna leży wyżej — ten incydent jest teraz skutkiem większej awarii
            incidents.close(conn, inc.id, "system", now=now,
                            text=f"Teraz to skutek awarii: {g.name(effect_of[root])} — zobacz nowy incydent.")
        elif root in planned:
            continue    # incydent sprzed przerwy zostaje — przerwa nie znaczy, że naprawione
        elif live.get(root, {}).get("state") != "down":
            last = history.last_change(conn, root)
            back_at = last["ts"] if last and last["state"] != "down" else now
            flaps = history.down_episodes(conn, root, now - FLAP_WINDOW)
            if flaps >= FLAP_COUNT and now - back_at < FLAP_CALM:
                incidents.mark_unstable(conn, inc.id, flaps, FLAP_WINDOW, FLAP_CALM, now)
                continue
            incidents.recover(conn, root, now=back_at, why=live.get(root, {}).get("why", ""))
    return findings
