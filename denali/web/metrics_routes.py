"""Stan dla innych programów: /metrics (Prometheus, Grafana) i /api/stan (JSON, np. czujnik REST w Home Assistancie).

Tylko odczyt, bez sesji: nagłówek „Authorization: Bearer <DENALI_METRICS_TOKEN>”. Bez tokenu w .env obu adresów
nie ma (404) — nic nie wycieka z panelu, dopóki świadomie tego nie włączysz.
"""
import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from .. import __version__, live, maintenance, updates
from ..db import kv_get
from ..models import check_state, device, incident
from .deps import NotFound

router = APIRouter()
UP = {"ok": 1, "warn": 1, "down": 0}        # off i unknown — bez wartości (nie wiemy albo wyłączone celowo)


class Unauthorized(Exception):
    pass


def _authorize(request: Request) -> None:
    token = request.app.state.settings.metrics_token
    if not token:
        raise NotFound()
    sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not sent or not hmac.compare_digest(sent, token):
        raise Unauthorized()


def snapshot(conn, now=None):
    now = now or time.time()
    states = live.saved(conn, now)
    breaks = maintenance.active(conn, now)
    beat = kv_get(conn, "worker.heartbeat")
    devices = [{"id": d.id, "name": d.name, "kind": d.kind, "criticality": d.criticality,
                "state": states.get(d.id, {}).get("state", "unknown"), "why": states.get(d.id, {}).get("why", ""),
                "maintenance_until": breaks[d.id]["ends_at"] if d.id in breaks else None}
               for d in conn.execute(select(device.c.id, device.c.name, device.c.kind, device.c.criticality)
                                     .order_by(device.c.name))]
    incidents = [dict(r) for r in conn.execute(
        select(incident.c.id, incident.c.title, incident.c.severity, incident.c.status, incident.c.started_at)
        .where(incident.c.status != "zamkniety").order_by(incident.c.started_at)).mappings()]
    checks = [dict(r) for r in conn.execute(
        select(check_state.c.device_id, check_state.c.kind, check_state.c.target, check_state.c.ok, check_state.c.ms,
               check_state.c.cert_expires_at)).mappings()]
    return {"at": now, "version": __version__, "worker_age_s": round(now - beat, 1) if beat else None,
            "devices": devices, "incidents": incidents, "checks": checks, "updates_pending": updates.count(conn)}


@router.get("/api/stan")
def state_json(request: Request):
    try:
        _authorize(request)
    except Unauthorized:
        return JSONResponse({"error": "brak lub zły token"}, 401, headers={"WWW-Authenticate": "Bearer"})
    with request.app.state.engine.connect() as conn:
        return snapshot(conn)


def _label(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def prometheus(snap) -> str:
    names = {d["id"]: d["name"] for d in snap["devices"]}
    out = ["# HELP denali_device_up 1 = działa (ok albo uwaga), 0 = nie działa. Brak = wyłączone albo brak danych.",
           "# TYPE denali_device_up gauge"]
    for d in snap["devices"]:
        if d["state"] in UP:
            out.append(f'denali_device_up{{device="{_label(d["name"])}",kind="{d["kind"]}"}} {UP[d["state"]]}')
    out += ["# HELP denali_device_state Stan urządzenia (jedna seria na stan, wartość 1 przy bieżącym).",
            "# TYPE denali_device_state gauge"]
    for d in snap["devices"]:
        out.append(f'denali_device_state{{device="{_label(d["name"])}",state="{d["state"]}"}} 1')
    out += ["# HELP denali_device_maintenance 1 = trwa planowana przerwa.", "# TYPE denali_device_maintenance gauge"]
    for d in snap["devices"]:
        out.append(f'denali_device_maintenance{{device="{_label(d["name"])}"}} {1 if d["maintenance_until"] else 0}')
    out += ["# HELP denali_check_ok Wynik sprawdzenia z LAN (1 = odpowiada).", "# TYPE denali_check_ok gauge",
            "# HELP denali_check_response_ms Czas odpowiedzi sprawdzenia w milisekundach.",
            "# TYPE denali_check_response_ms gauge",
            "# HELP denali_cert_expiry_timestamp_seconds Kiedy wygasa certyfikat (czas UNIX).",
            "# TYPE denali_cert_expiry_timestamp_seconds gauge"]
    for c in snap["checks"]:
        lbl = f'device="{_label(names.get(c["device_id"], c["device_id"]))}",kind="{c["kind"]}",target="{_label(c["target"])}"'
        out.append(f"denali_check_ok{{{lbl}}} {1 if c['ok'] else 0}")
        if c["ms"] is not None:
            out.append(f"denali_check_response_ms{{{lbl}}} {c['ms']:.1f}")
        if c["cert_expires_at"]:
            out.append(f"denali_cert_expiry_timestamp_seconds{{{lbl}}} {c['cert_expires_at']:.0f}")
    out += ["# HELP denali_incidents_open Otwarte incydenty.", "# TYPE denali_incidents_open gauge"]
    for sev in ("krytyczny", "ostrzezenie", "info"):
        n = sum(1 for i in snap["incidents"] if i["severity"] == sev)
        out.append(f'denali_incidents_open{{severity="{sev}"}} {n}')
    out += ["# HELP denali_updates_pending Urządzenia czekające na aktualizację.", "# TYPE denali_updates_pending gauge",
            f"denali_updates_pending {snap['updates_pending']}"]
    if snap["worker_age_s"] is not None:
        out += ["# HELP denali_worker_heartbeat_age_seconds Ile sekund temu worker dał znak życia.",
                "# TYPE denali_worker_heartbeat_age_seconds gauge",
                f"denali_worker_heartbeat_age_seconds {snap['worker_age_s']}"]
    return "\n".join(out) + "\n"


@router.get("/metrics")
def metrics(request: Request):
    try:
        _authorize(request)
    except Unauthorized:
        return PlainTextResponse("brak lub zły token\n", 401, headers={"WWW-Authenticate": "Bearer"})
    with request.app.state.engine.connect() as conn:
        snap = snapshot(conn)
    return PlainTextResponse(prometheus(snap), media_type="text/plain; version=0.0.4; charset=utf-8")
