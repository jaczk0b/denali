"""Eksport Markdown do diagnozy: dla siebie albo do wklejenia modelowi językowemu.

Co jest nie tak, urządzenia, zależności, ostatnie zmiany i incydenty. Bez skarbca i plików, a notatki i runbooki
przechodzą przez maskowanie sekretów: nazwy zadań i hasła wpisane w notatkach nie wychodzą z panelu.
"""
import time
from datetime import datetime

from sqlalchemy import select

from . import alerts, changes, incidents, inventory as inv, live
from .docs import runbooks_for_device
from .masking import mask
from .models import dependency, device, device_address

STATE_WORD = {"ok": "działa", "warn": "uwaga", "down": "nie działa", "off": "wyłączone", "unknown": "brak danych"}


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def _cell(text):
    return mask(str(text or "")).replace("|", "\\|").replace("\n", " ")


def _problems(conn, device_id=None):
    lines = []
    for inc in incidents.list_incidents(conn, open_only=True, device_id=device_id):
        lines.append(f"- **Incydent:** {_cell(inc['title'])} (od {_when(inc['started_at'])})")
    for a in alerts.active(conn, device_id=device_id):
        lines.append(f"- **{a['label']}** ({a['device'] or 'cała lokalizacja'}): {_cell(a['message'])}")
    return lines or ["- Nic nie zgłoszono."]


def overview(conn, now=None):
    """Cała infrastruktura."""
    now = now or time.time()
    states = live.saved(conn, now)
    devs = conn.execute(select(device).order_by(device.c.kind, device.c.name)).mappings().all()
    names = {d["id"]: d["name"] for d in devs}
    ips = {}
    for a in conn.execute(select(device_address).where(device_address.c.kind == "ip")):
        ips.setdefault(a.device_id, a.value)
    out = [f"# Denali: stan na {_when(now)}", "", "## Co jest nie tak", *_problems(conn), "", "## Urządzenia", "",
           "| Nazwa | Typ | Stan | Na czym działa | Adres | Do czego służy |", "|---|---|---|---|---|---|"]
    for d in devs:
        st = (states.get(d["id"]) or {}).get("state", "unknown")
        out.append(f"| {_cell(d['name'])} | {d['kind']} | {STATE_WORD.get(st, st)} | "
                   f"{_cell(names.get(d['parent_id'], '—'))} | {_cell(ips.get(d['id'], '—'))} | {_cell(d['role'])} |")
    deps = conn.execute(select(dependency)).mappings().all()
    if deps:
        out += ["", "## Zależności", ""]
        out += [f"- {_cell(names.get(x['device_id'], '?'))} → {_cell(names.get(x['depends_on_id'], '?'))} "
                f"({x['type']}, {'twarda' if x['hard'] else 'miękka'})" for x in deps]
    return "\n".join(out) + "\n"


def for_device(conn, device_id, now=None):
    """Jedno urządzenie: to, co trzeba wiedzieć, żeby je zdiagnozować."""
    now = now or time.time()
    d = inv.get_device(conn, device_id)
    state = (live.saved(conn, now).get(device_id) or {})
    out = [f"# {_cell(d['name'])}", "", f"- Typ: {d['kind']}", f"- Stan: {STATE_WORD.get(state.get('state'), 'brak danych')}"]
    if state.get("why"):
        out.append(f"- Powód: {_cell(state['why'])}")
    if d["role"]:
        out.append(f"- Do czego służy: {_cell(d['role'])}")
    out += ["", "## Co jest nie tak", *_problems(conn, device_id)]
    events = changes.recent(conn, device_id, now - 7 * 86400, limit=40)
    if events:
        out += ["", "## Zmiany z ostatnich 7 dni", ""]
        out += [f"- {_when(e['ts'])}: {_cell(e['text'])}" for e in events]
    history = incidents.list_incidents(conn, device_id=device_id, limit=10)
    if history:
        out += ["", "## Ostatnie incydenty", ""]
        out += [f"- {_when(i['started_at'])}: {_cell(i['title'])}" for i in history]
    for r in runbooks_for_device(conn, device_id):
        out += ["", f"## Runbook: {_cell(r['title'])}", "", mask(r["body_md"])]
    if d["notes_md"]:
        out += ["", "## Notatki", "", mask(d["notes_md"])]
    return "\n".join(out) + "\n"
