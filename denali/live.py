"""Stan urządzeń na żywo i automatyczne incydenty.

Stan urządzenia łączy wszystkie źródła:
- integracje (np. Proxmox): ok | warn | down | off | unknown,
- sprawdzenia z LAN (ping, HTTP): „nie działa” dopiero, gdy nie odpowiada dłużej niż próg z profilu
  (down_after_s) — krótka czkawka nie robi alarmu.

Pierwszeństwo: off (wyłączone celowo — wtedy milczący ping nie jest awarią) → down → warn → ok → unknown.
Worker co minutę liczy stany, zapisuje je (strony panelu tylko je czytają), puszcza analizę przyczyny
i otwiera albo zamyka incydenty.
"""
import time

from sqlalchemy import select

from . import incidents, inventory as inv, profiles
from .db import kv_get, kv_set
from .models import check_state, device, external_ref, incident, observed_state

STATES_KEY = "live.states"
STALE_AFTER = 300           # stan starszy niż 5 min = worker nie działa → nie pokazujemy go jako aktualnego


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
    for d in conn.execute(select(device.c.id)).scalars():
        if not profiles.value(conn, d, "monitor"):
            continue
        down_after = profiles.value(conn, d, "down_after_s")
        states = list(from_sources.get(d, []))
        for c in from_checks.get(d, []):
            if c["ok"]:
                soon = c["cert_expires_at"] and c["cert_expires_at"] - now < 14 * 86400
                states.append(("warn" if soon else "ok", c["summary"]))
            elif c["first_fail_at"] and now - c["first_fail_at"] >= down_after:
                states.append(("down", c["summary"]))
            else:
                states.append(("unknown", f"{c['summary']} (czekam {down_after} s, zanim uznam za awarię)"))
        if not states:
            continue
        kinds = [s for s, _ in states]
        for wanted in ("off", "down", "warn", "ok", "unknown"):
            if wanted in kinds:
                out[d] = {"state": wanted, "why": "; ".join(w for s, w in states if s == wanted)[:300]}
                break
    return out


def saved(conn, now=None):
    """Stany zapisane przez workera: {device_id: {"state", "why"}} albo {} gdy nieaktualne."""
    now = now or time.time()
    data = kv_get(conn, STATES_KEY) or {}
    if not data or now - data.get("at", 0) > STALE_AFTER:
        return {}
    return {int(k): v for k, v in data.get("devices", {}).items()}


def evaluate(conn, now=None):
    """Liczy stany, zapisuje je i aktualizuje incydenty. Zwraca listę ustaleń analizy przyczyny."""
    now = now or time.time()
    live = compute(conn, now)
    kv_set(conn, STATES_KEY, {"at": now, "devices": {str(k): v for k, v in live.items()}})
    g = inv.load_graph(conn)
    findings = g.analyze({d: v["state"] for d, v in live.items()})
    causes = {f.cause for f in findings}
    effect_of = {e: f.cause for f in findings for e in f.effects}
    for f in findings:
        incidents.record_finding(conn, g, f, now)
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
        elif live.get(root, {}).get("state") != "down":
            incidents.recover(conn, root, now=now)
    return findings
