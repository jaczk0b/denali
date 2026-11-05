"""Pulpit, infrastruktura (lista, karta urządzenia, edycja), kolejka „do zatwierdzenia”, aktualizacje, wyszukiwarka.

Odczyt: każdy zalogowany. Zmiany: tylko administrator (require_admin sprawdza też CSRF).
"""
import time
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select

from .. import (actions, alerts, auth, changes, checks, configs, desk, deskview, discovery, docs, files, forecast, history, incidents, infra_views, integrations,
               inventory as inv, live, maintenance, profiles, racks, status, tablet, terms, upcoming, updates, vault)
from ..db import kv_get
from ..models import (cable, dependency, device, device_address, external_ref, incident, integration, procedure,
                      procedure_step, runbook, setting)
from ..templating import render
from .deps import NotFound, client_ip, require_admin, require_fresh, require_user

router = APIRouter()
EDIT_SETTINGS = ("monitor", "down_after_s", "notify", "notify_warn", "disk_warn", "disk_crit", "updates")   # akcje: osobny formularz (action_routes)
DAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]


def _back(url):
    return RedirectResponse(url, 303)


def _int(v):
    try:
        return int(v) if v is not None and str(v).strip() else None
    except (TypeError, ValueError):
        return None


# ───────────────────────── pulpit ─────────────────────────
@router.get("/")
def pulpit(request: Request, edycja: str = "", dodaj: str = "", sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        devices = deskview.device_rows(conn)
        names = {d["id"]: d["name"] for d in devices}
        marks = status.device_marks(conn)
        states = live.saved(conn)
        open_inc = status.open_incidents(conn)
        effects = {i["id"]: conn.execute(
            select(func.count()).select_from(incidents.incident_device)
            .where(incidents.incident_device.c.incident_id == i["id"],
                   incidents.incident_device.c.role.in_(("skutek", "gorzej")))).scalar() for i in open_inc}
        last_closed = conn.execute(select(incident.c.ended_at).where(incident.c.status == "zamkniety")
                                   .order_by(incident.c.ended_at.desc()).limit(1)).mappings().first()
        checks_now = deskview.load_checks(conn)
        board = deskview.decorate(desk.board(conn, sess["role"]), marks, states, checks_now)
        banner = deskview.banner(conn, open_inc, states, names)
        settings = desk.look(conn)
        rows, more, good = deskview.attention(conn, open_inc, devices, states)
        edit = edycja == "1" and sess["role"] == "admin" and sess["kind"] != "kiosk"
        ctx = {
            "board": board, "sections": desk.sections(board, settings), "marks": marks, "open_incidents": open_inc, "effects": effects,
            "layers": [l for l in status.layers(devices, marks, states) if l["state"] != "none"],
            "no_devices": not devices, "has_live": bool(states),
            "verdict": status.verdict(devices, states, open_inc, maintenance.active(conn), last_closed),
            "banner": banner, "chain": deskview.chain(conn, banner, states, names),
            "kpis": deskview.kpis(conn, devices, states, checks_now),
            "widgets": deskview.widgets(conn, desk.widgets(conn), {k: v[0] for k, v in desk.WIDGETS.items()}),
            "attention": rows, "attention_more": more, "attention_good": good,
            "events": deskview.events(conn), "upcoming": upcoming.items(conn),
            "side": desk.side(conn), "look": settings, "edit": edit,
            "suggest": desk.suggestions(conn)[:8] if sess["role"] == "admin" else [],
        }
        if edit:
            ctx.update(catalog=desk.catalog(conn), open_add=_int(dodaj), layout=desk.snapshot(conn),
                       live_version="")          # odświeżanie na żywo przerwałoby przeciąganie
    now = datetime.now()
    ctx.update(clock=now.strftime("%H:%M"), date=f"{DAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}")
    if sess["kind"] == "kiosk":
        with request.app.state.engine.begin() as conn:
            ctx.update(_wall(request, conn, sess, devices, states, open_inc))
        return render(request, "sciana.html", ctx)
    return render(request, "pulpit.html", ctx)


def _wall(request, conn, sess, devices, states, open_inc):
    """Tablet na ścianie: statystyki, wykres pingu do sieci, ustawienia tego tabletu i — po PIN-ie — narzędzia."""
    flash_key = f"tablet.flash.{sess['public_id']}"
    flash = kv_get(conn, flash_key)
    if flash:
        conn.execute(setting.delete().where(setting.c.key == flash_key))
    counts = {s: sum(1 for v in states.values() if v["state"] == s) for s in ("ok", "warn", "down", "off")}
    spark = None
    for d in sorted((d for d in devices if d["kind"] in ("modem", "router", "firewall")), key=lambda d: d["kind"]):
        rows = history.response_times(conn, d["id"], "ping", 24)
        if rows:
            now = time.time()
            spark = {"name": d["name"], **history.sparkline(rows, now - 86400, now, width=600, height=70)}
            break
    return {
        "prefs": tablet.prefs(request.cookies.get(tablet.PREFS_COOKIE)), "flash": flash, "spark": spark,
        "stats": {**counts, "total": len(devices), "incidents": len(open_inc)},
        # potwierdzona albo wyciszona awaria nie alarmuje: widać ją, ale nie krzyczy
        "alarm_ids": " ".join(str(i["id"]) for i in open_inc if i["status"] == "trwa" and not deskview.muted(i, time.time())),
        "has_pin": bool(tablet.pin_info(conn, sess["public_id"])),
        "hosts": [d for d in devices if d["kind"] == "host"],
    }


# ───────────────────────── aktualizacje ─────────────────────────
@router.get("/aktualizacje")
def updates_list(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        rows = updates.overview(conn)
        used = {s for r in rows for s in r["sources"]}
        stale = sorted(r.name for r in conn.execute(select(integration.c.name, integration.c.last_error))
                       if r.last_error and r.name in used)
    return render(request, "aktualizacje.html", {"rows": rows, "stale": stale})


# ───────────────────────── lista i karta ─────────────────────────
@router.get("/urzadzenia")
def device_list(request: Request, q: str = "", stan: str = "", sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(select(device).order_by(device.c.name)).mappings().all()
        marks = status.device_marks(conn)
        states = live.saved(conn)
        ips = {r.device_id: r.value for r in conn.execute(
            select(device_address.c.device_id, device_address.c.value)
            .where(device_address.c.kind == "ip").order_by(device_address.c.is_primary))}
        names = {r["id"]: r["name"] for r in rows}
        tabs = infra_views.tabs(conn)
    total = len(rows)
    needle = q.strip().lower()
    if needle:
        rows = [d for d in rows if needle in d["name"].lower() or needle in (d["role"] or "").lower()
                or needle in ips.get(d["id"], "")]
    if stan in ("ok", "warn", "down", "off", "unknown"):
        rows = [d for d in rows if states.get(d["id"], {}).get("state", "unknown") == stan]
    groups = []
    for key, label, _k in status.LAYERS + [("inne", "Inne", set())]:
        devs = [d for d in rows if status.layer_of(d["kind"]) == key]
        if devs:
            groups.append((label, devs))
    return render(request, "urzadzenia.html", {"groups": groups, "marks": marks, "ips": ips, "names": names,
                                               "states": states, "tabs": tabs, "q": q, "stan": stan,
                                               "shown": len(rows), "total": total})


def _card(request, conn, device_id, *, error=None, message=None, revealed=None, status_code=200):
    d = inv.get_device(conn, device_id)
    if not d:
        raise NotFound("Nie ma takiego urządzenia.")
    g = inv.load_graph(conn)
    hard, soft = g.impact(device_id)
    all_devices = conn.execute(select(device.c.id, device.c.name, device.c.kind).order_by(device.c.name)
                               ).mappings().all()
    profile_name, settings = profiles.effective(conn, device_id)
    is_admin = request.state.sess["role"] == "admin"
    ctx = {
        "d": d, "error": error, "message": message, "revealed": revealed,
        "parent": inv.get_device(conn, d["parent_id"]) if d["parent_id"] else None,
        "children": conn.execute(select(device.c.id, device.c.name, device.c.kind)
                                 .where(device.c.parent_id == device_id).order_by(device.c.name)).mappings().all(),
        "addresses": conn.execute(select(device_address).where(device_address.c.device_id == device_id)
                                  .order_by(device_address.c.kind, device_address.c.value)).mappings().all(),
        "deps_up": conn.execute(select(dependency, device.c.name).join(device, device.c.id == dependency.c.depends_on_id)
                                .where(dependency.c.device_id == device_id)).mappings().all(),
        "deps_down": conn.execute(select(dependency, device.c.name).join(device, device.c.id == dependency.c.device_id)
                                  .where(dependency.c.depends_on_id == device_id)).mappings().all(),
        "cables": inv.device_cables(conn, device_id),
        "impact_hard": sorted(g.name(x) for x in hard), "impact_soft": sorted(g.name(x) for x in soft),
        "names": {x["id"]: x["name"] for x in all_devices}, "all_devices": all_devices,
        "profile_name": profile_name, "settings": settings, "setting_defs": profiles.SETTINGS,
        "edit_settings": EDIT_SETTINGS,
        "procedures": docs.procedures_for_device(conn, device_id),
        "runbooks": docs.runbooks_for_device(conn, device_id),
        "incidents": incidents.list_incidents(conn, device_id=device_id, limit=20),
        "files": files.for_device(conn, device_id),
        "secrets": vault.list_secrets(conn, role="admin", device_id=device_id) if is_admin else [],
        "config_backups": configs.for_device(conn, device_id) if is_admin else [],
        "marks": (marks := status.device_marks(conn)), "mark": marks.get(device_id),
        "sources": integrations.states_for_device(conn, device_id),
        "checks": checks.for_device(conn, device_id), "now": time.time(),
        "live": live.saved(conn).get(device_id),
        "updates": updates.for_device(conn, device_id), "rack_pos": racks.for_device(conn, device_id),
        "refs": conn.execute(select(external_ref).where(external_ref.c.device_id == device_id)).mappings().all(),
        "gone": discovery.gone(conn, device_id),
        "dev_actions": actions.for_device(conn, device_id) if is_admin else [],
        "action_keys": actions.known_for_device(conn, device_id) if is_admin else [],
        "action_runs": actions.recent(conn, device_id, 5) if is_admin else [],
        "action_labels": {k: f"{c.label}: {a.label}" for k, (c, a) in actions.catalog().items()},
        "actions_on": actions.enabled(conn),
        "history": _history(conn, device_id),
        "storage": [f for f in forecast.forecasts(conn) if f["device_id"] == device_id],
        "breaks": [m for m in maintenance.active(conn).values() if m["device_id"] == device_id],
        "break_choices": maintenance.CHOICES,
        "desk_place": desk.device_category(conn, device_id),
        "open_alerts": alerts.active(conn, device_id=device_id), "terms": terms.for_device(conn, device_id),
        "custom_checks": checks.custom_for_device(conn, device_id), "custom_kinds": checks.CUSTOM_KINDS,
        "recent_changes": changes.recent(conn, device_id, time.time() - 30 * 86400),
    }
    return render(request, "urzadzenie.html", ctx, status_code)


def _history(conn, device_id, now=None):
    """Pasek dostępności (24 h i 7 dni) i wykres czasu odpowiedzi z ostatniej doby."""
    now = now or time.time()
    lines = []
    for kind, label in (("ping", "Ping z LAN"), ("http", "Aplikacja (HTTP)"), ("tcp", "Porty TCP")):
        spark = history.sparkline(history.response_times(conn, device_id, kind, 24, now), now - 86400, now)
        if spark:
            lines.append({"kind": kind, "label": label, **spark})
    day, week = history.availability(conn, device_id, 24, now), history.availability(conn, device_id, 168, now)
    if day["pct"] is None and not lines:
        return None
    return {"day": day, "week": week, "lines": lines}


@router.get("/urzadzenia/{device_id:int}")
def device_card(request: Request, device_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return _card(request, conn, device_id)


def _edit_form(request, conn, d, error=None, status_code=200):
    all_devices = conn.execute(select(device.c.id, device.c.name, device.c.kind).order_by(device.c.name)
                               ).mappings().all()
    profs = conn.execute(select(profiles.profile.c.id, profiles.profile.c.name).order_by(profiles.profile.c.name)
                         ).mappings().all()
    return render(request, "urzadzenie_edycja.html", {"d": d, "error": error, "all_devices": all_devices,
                                                      "profiles": profs, "nest": inv.NEST_IN,
                                                      "desk_categories": desk.categories(conn)}, status_code)


@router.get("/urzadzenia/nowe")
def device_new_form(request: Request, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        return _edit_form(request, conn, {"id": None, "name": "", "kind": "usluga", "role": "", "criticality": 3,
                                          "parent_id": None, "profile_id": None, "place": "", "serial": "",
                                          "notes_md": "", "icon": "", "app_url": "", "desk_category": None,
                                          "tcp_ports": ""})


def _device_fields(form):
    return {
        "name": form.get("name", ""), "kind": form.get("kind", ""), "role": form.get("role", ""),
        "criticality": _int(form.get("criticality")) or 3, "parent_id": _int(form.get("parent_id")),
        "profile_id": _int(form.get("profile_id")), "place": form.get("place", ""),
        "serial": form.get("serial", ""), "notes_md": form.get("notes_md", ""), "icon": form.get("icon", ""),
        "app_url": form.get("app_url", ""),
        "tcp_ports": form.get("tcp_ports", ""),
    }


@router.post("/urzadzenia/nowe")
async def device_create(request: Request, sess: dict = Depends(require_admin)):
    form = await request.form()
    fields, category = _device_fields(form), _int(form.get("desk_category"))
    with request.app.state.engine.begin() as conn:
        try:
            did = inv.create_device(conn, **fields)
            if category:
                desk.pin_device(conn, did, category)
        except (inv.InventoryError, desk.DeskError) as e:
            return _edit_form(request, conn, {"id": None, **fields, "desk_category": category}, str(e), 400)
        auth.audit(conn, "device_create", user=sess["login"], ip=client_ip(request), target=fields["name"])
    return _back(f"/urzadzenia/{did}")


@router.get("/urzadzenia/{device_id}/edycja")
def device_edit_form(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        d = inv.get_device(conn, device_id)
        if not d:
            raise NotFound("Nie ma takiego urządzenia.")
        place = desk.device_category(conn, device_id)
        return _edit_form(request, conn, {**d, "desk_category": place["id"] if place else None})


@router.post("/urzadzenia/{device_id}/edycja")
async def device_update(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    form = await request.form()
    fields, category = _device_fields(form), _int(form.get("desk_category"))
    with request.app.state.engine.begin() as conn:
        if not inv.get_device(conn, device_id):
            raise NotFound("Nie ma takiego urządzenia.")
        try:
            inv.update_device(conn, device_id, **fields)
            if "desk_category" in form:             # brak pola to nie „zdejmij z pulpitu”
                desk.pin_device(conn, device_id, category)
        except (inv.InventoryError, desk.DeskError) as e:
            return _edit_form(request, conn, {"id": device_id, **fields, "desk_category": category}, str(e), 400)
        auth.audit(conn, "device_update", user=sess["login"], ip=client_ip(request), target=fields["name"])
    return _back(f"/urzadzenia/{device_id}")


@router.post("/urzadzenia/{device_id}/usun")
def device_delete(request: Request, device_id: int, confirm: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        d = inv.get_device(conn, device_id)
        if not d:
            raise NotFound("Nie ma takiego urządzenia.")
        if confirm.strip() != d["name"]:
            return _card(request, conn, device_id, error="Żeby usunąć, przepisz dokładnie nazwę urządzenia.",
                         status_code=400)
        inv.delete_device(conn, device_id)
        files.cleanup_orphans(conn, request.app.state.settings.data_dir)
        auth.audit(conn, "device_delete", user=sess["login"], ip=client_ip(request), target=d["name"])
    return _back("/urzadzenia")


# ───────────────────────── adresy, zależności, kable ─────────────────────────
def _card_change(request, device_id, fn, audit_action, sess, target=""):
    """Wspólny schemat zmian na karcie: wykonaj, przy błędzie pokaż kartę z komunikatem, inaczej wróć na kartę."""
    with request.app.state.engine.begin() as conn:
        if not inv.get_device(conn, device_id):
            raise NotFound("Nie ma takiego urządzenia.")
        try:
            fn(conn)
        except (inv.InventoryError, profiles.ProfileError, files.FileError, ValueError) as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
        auth.audit(conn, audit_action, user=sess["login"], ip=client_ip(request), target=target or str(device_id))
    return _back(f"/urzadzenia/{device_id}")


@router.get("/urzadzenia/{device_id:int}/restart-hosta")
def host_restart(request: Request, device_id: int, sess: dict = Depends(require_admin)):
    """Lista kroków bezpiecznego restartu serwera. Każde wyłączenie i uruchomienie idzie zwykłą akcją
    (te same zamki i potwierdzenia); sam restart węzła robisz w Proxmoxie — panel nie ma do tego uprawnień."""
    with request.app.state.engine.connect() as conn:
        d = inv.get_device(conn, device_id)
        if not d or d["kind"] != "host":
            raise NotFound("To nie jest serwer fizyczny.")
        g = inv.load_graph(conn)
        plan = maintenance.host_plan(conn, g, device_id)
        acts = {}
        for guest in plan["shutdown"]:
            acts[guest["id"]] = {o["key"].split(".")[-1]: o for o in actions.for_device(conn, guest["id"])
                                 if o["key"].split(".")[-1] in ("shutdown", "start") and not o["reason"]}
        ctx = {"d": d, "plan": plan, "acts": acts, "live": live.saved(conn),
               "breaks": [m for m in maintenance.active(conn).values() if m["device_id"] == device_id]}
    return render(request, "restart_hosta.html", ctx)


@router.post("/urzadzenia/{device_id}/przerwa")
def break_start(request: Request, device_id: int, minutes: str = Form(""), reason: str = Form(""),
                sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: maintenance.start(c, device_id, minutes, reason, sess["login"]),
                        "maintenance_start", sess, f"{device_id}: {minutes} min")


@router.post("/urzadzenia/{device_id}/przerwa/{break_id}/koniec")
def break_end(request: Request, device_id: int, break_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: maintenance.end(c, break_id, device_id), "maintenance_end", sess)


@router.post("/urzadzenia/{device_id}/adresy")
def address_add(request: Request, device_id: int, kind: str = Form(""), value: str = Form(""),
                primary: str = Form(""), sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: inv.add_address(c, device_id, kind, value, primary=bool(primary)),
                        "address_add", sess, value)


@router.post("/urzadzenia/{device_id}/adresy/{address_id}/usun")
def address_remove(request: Request, device_id: int, address_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: c.execute(device_address.delete().where(
        device_address.c.id == address_id, device_address.c.device_id == device_id)), "address_remove", sess)


@router.post("/urzadzenia/{device_id}/zaleznosci")
def dependency_add(request: Request, device_id: int, depends_on_id: str = Form(""), type: str = Form("siec"),
                   hard: str = Form(""), note: str = Form(""), sess: dict = Depends(require_admin)):
    def add(c):
        target = _int(depends_on_id)
        if not target:
            raise inv.InventoryError("Wybierz urządzenie.")
        inv.add_dependency(c, device_id, target, type, hard=bool(hard), note=note)
    return _card_change(request, device_id, add, "dependency_add", sess)


@router.post("/urzadzenia/{device_id}/zaleznosci/{dep_id}/usun")
def dependency_remove(request: Request, device_id: int, dep_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: c.execute(dependency.delete().where(
        dependency.c.id == dep_id, or_(dependency.c.device_id == device_id, dependency.c.depends_on_id == device_id))),
        "dependency_remove", sess)


@router.post("/urzadzenia/{device_id}/kable")
def cable_add(request: Request, device_id: int, other_id: str = Form(""), side: str = Form("b"),
              my_port: str = Form(""), other_port: str = Form(""), label: str = Form(""),
              sess: dict = Depends(require_admin)):
    """side=b: to urządzenie jest wpięte w drugie (drugie bliżej routera); side=a: odwrotnie."""
    def add(c):
        other = _int(other_id)
        if not other:
            raise inv.InventoryError("Wybierz drugie urządzenie.")
        if side == "a":
            inv.add_cable(c, device_id, other, my_port, other_port, label)
        else:
            inv.add_cable(c, other, device_id, other_port, my_port, label)
    return _card_change(request, device_id, add, "cable_add", sess)


@router.post("/urzadzenia/{device_id}/kable/{cable_id}/usun")
def cable_remove(request: Request, device_id: int, cable_id: int, sess: dict = Depends(require_admin)):
    return _card_change(request, device_id, lambda c: c.execute(cable.delete().where(
        cable.c.id == cable_id, or_(cable.c.a_device_id == device_id, cable.c.b_device_id == device_id))),
        "cable_remove", sess)


# ───────────────────────── ustawienia (wyjątki od profilu) ─────────────────────────
def parse_setting(key, raw):
    if key not in profiles.SETTINGS:
        raise profiles.ProfileError("Nieznane ustawienie.")
    typ = profiles.SETTINGS[key][0]
    if typ is bool:
        return raw == "tak"
    if typ is int:
        try:
            return int(raw)
        except ValueError:
            raise profiles.ProfileError("Podaj liczbę sekund.") from None
    return raw


@router.post("/urzadzenia/{device_id}/ustawienia")
def setting_change(request: Request, device_id: int, key: str = Form(""), value: str = Form(""),
                   clear: str = Form(""), sess: dict = Depends(require_admin)):
    def change(c):
        if key not in EDIT_SETTINGS:
            raise profiles.ProfileError("Tego ustawienia nie zmienia się tutaj.")
        if clear:
            profiles.clear_override(c, device_id, key)
        else:
            profiles.set_override(c, device_id, key, parse_setting(key, value))
    return _card_change(request, device_id, change, "setting_override", sess, key)


# ───────────────────────── pliki ─────────────────────────
@router.post("/urzadzenia/{device_id}/pliki")
async def file_upload(request: Request, device_id: int, plik: UploadFile = File(...),
                      sess: dict = Depends(require_admin)):
    data = await plik.read(files.MAX_SIZE + 1)
    return _card_change(request, device_id, lambda c: files.store(
        c, request.app.state.settings.data_dir, device_id, plik.filename, data, plik.content_type or "",
        user=sess["login"]), "file_upload", sess, plik.filename or "")


@router.get("/pliki/{file_id}")
def file_download(request: Request, file_id: int, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        found = files.get(conn, request.app.state.settings.data_dir, file_id)
    if not found or not found[1].exists():
        raise NotFound("Nie ma takiego pliku.")
    row, path = found
    # zawsze jako załącznik: wgrany HTML/SVG nie wykona się w kontekście panelu
    return FileResponse(path, media_type=row["mime"], filename=row["name"], content_disposition_type="attachment")


@router.post("/urzadzenia/{device_id}/pliki/{file_id}/usun")
def file_remove(request: Request, device_id: int, file_id: int, sess: dict = Depends(require_admin)):
    def rm(c):
        found = files.get(c, request.app.state.settings.data_dir, file_id)
        if found and found[0]["device_id"] == device_id:
            files.remove(c, request.app.state.settings.data_dir, file_id)
    return _card_change(request, device_id, rm, "file_remove", sess)


# ───────────────────────── kopie konfiguracji ─────────────────────────
@router.post("/urzadzenia/{device_id}/kopie/{backup_id}/pobierz")
async def config_backup_download(request: Request, device_id: int, backup_id: int,
                                 sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)      # archiwum zawiera hasła Wi-Fi
    with request.app.state.engine.begin() as conn:
        try:
            name, data = configs.download(conn, request.app.state.box, device_id, backup_id, role=sess["role"],
                                          reauth_fresh=auth.reauth_fresh(sess), user=sess["login"],
                                          ip=client_ip(request))
        except LookupError as e:
            raise NotFound(str(e)) from None
        except PermissionError as e:
            return _card(request, conn, device_id, error=str(e), status_code=403)
    return Response(data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ───────────────────────── skarbiec ─────────────────────────
@router.post("/urzadzenia/{device_id}/skarbiec")
def secret_add(request: Request, device_id: int, label: str = Form(""), username: str = Form(""),
               secret: str = Form(""), note: str = Form(""), sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            vault.add(conn, request.app.state.box, label, secret, role=sess["role"], user=sess["login"],
                      ip=client_ip(request), device_id=device_id, username=username, note=note)
        except ValueError as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
    return _back(f"/urzadzenia/{device_id}#skarbiec")


@router.post("/urzadzenia/{device_id}/skarbiec/{secret_id}/odslon")
async def secret_reveal(request: Request, device_id: int, secret_id: int, sess: dict = Depends(require_admin)):
    await require_fresh(request, sess)
    with request.app.state.engine.begin() as conn:
        try:
            plain = vault.reveal(conn, request.app.state.box, secret_id, role=sess["role"],
                                 reauth_at=sess["reauth_at"], user=sess["login"], ip=client_ip(request))
        except (ValueError, PermissionError) as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
        return _card(request, conn, device_id, revealed={"id": secret_id, "value": plain})


@router.post("/urzadzenia/{device_id}/skarbiec/{secret_id}/usun")
def secret_remove(request: Request, device_id: int, secret_id: int, sess: dict = Depends(require_admin)):
    with request.app.state.engine.begin() as conn:
        try:
            vault.remove(conn, secret_id, role=sess["role"], user=sess["login"], ip=client_ip(request))
        except ValueError as e:
            return _card(request, conn, device_id, error=str(e), status_code=400)
    return _back(f"/urzadzenia/{device_id}#skarbiec")


# ───────────────────────── kolejka „do zatwierdzenia” ─────────────────────────
def _queue_page(request, conn, error=None, status_code=200):
    sources = {r.id: r.name for r in conn.execute(select(integration.c.id, integration.c.name))}
    all_devices = conn.execute(select(device.c.id, device.c.name).order_by(device.c.name)).mappings().all()
    return render(request, "kolejka.html", {"rows": discovery.pending(conn), "gone": discovery.gone(conn),
                                            "integrations": sources,
                                            "matches": discovery.suggest_matches(conn),
                                            "all_devices": all_devices, "error": error}, status_code)


@router.get("/zatwierdz")
def queue(request: Request, sess: dict = Depends(require_user)):
    with request.app.state.engine.connect() as conn:
        return _queue_page(request, conn)


def _queue_action(request, sess, fn):
    with request.app.state.engine.begin() as conn:
        try:
            fn(conn)
        except inv.InventoryError as e:
            return _queue_page(request, conn, str(e), 400)
    return _back("/zatwierdz")


@router.post("/zatwierdz/dopasowane")
def queue_link_matches(request: Request, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.link_matches(c, user=sess["login"], ip=client_ip(request)))


@router.post("/zatwierdz/wybrane")
async def queue_apply(request: Request, sess: dict = Depends(require_admin)):
    form = await request.form()
    choices = {}
    for key, val in form.multi_items():
        rid = _int(key[2:]) if key.startswith("d_") else None
        val = str(val)
        if not rid or not val:
            continue                    # „nie ruszaj” — zostaje w kolejce
        if val in ("nowe", "pomin"):
            choices[rid] = val
        elif val.startswith("dev:") and _int(val[4:]):
            choices[rid] = _int(val[4:])
    gone = {_int(k[2:]): str(v) for k, v in form.multi_items() if k.startswith("g_") and _int(k[2:]) and v}

    def apply(c):
        if not choices and not gone:
            raise inv.InventoryError("Nic nie wybrano — przy każdym obiekcie wybierz, co z nim zrobić.")
        discovery.resolve_gone(c, gone, user=sess["login"], ip=client_ip(request))
        discovery.apply_choices(c, choices, user=sess["login"], ip=client_ip(request))
        files.cleanup_orphans(c, request.app.state.settings.data_dir)
    return _queue_action(request, sess, apply)


@router.post("/zatwierdz/wszystko")
def queue_approve_all(request: Request, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.approve_all(c, user=sess["login"], ip=client_ip(request)))


@router.post("/zatwierdz/{ref_id}")
def queue_approve(request: Request, ref_id: int, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.approve(c, ref_id, user=sess["login"],
                                                                    ip=client_ip(request)))


@router.post("/zatwierdz/{ref_id}/odrzuc")
def queue_ignore(request: Request, ref_id: int, sess: dict = Depends(require_admin)):
    return _queue_action(request, sess, lambda c: discovery.ignore(c, ref_id, user=sess["login"],
                                                                   ip=client_ip(request)))


@router.post("/zatwierdz/{ref_id}/przypnij")
def queue_link(request: Request, ref_id: int, device_id: str = Form(""), sess: dict = Depends(require_admin)):
    def link(c):
        target = _int(device_id)
        if not target:
            raise inv.InventoryError("Wybierz urządzenie, do którego przypiąć.")
        discovery.link(c, ref_id, target, user=sess["login"], ip=client_ip(request))
    return _queue_action(request, sess, link)


# ───────────────────────── wyszukiwarka ─────────────────────────
def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search(conn, q):
    res = {"devices": [], "procedures": [], "runbooks": [], "incidents": []}
    if not q:
        return res
    pat = _like(q)
    by_addr = select(device_address.c.device_id).where(device_address.c.value.ilike(pat, escape="\\"))
    res["devices"] = conn.execute(select(device).where(or_(
        device.c.name.ilike(pat, escape="\\"), device.c.role.ilike(pat, escape="\\"),
        device.c.place.ilike(pat, escape="\\"), device.c.id.in_(by_addr))).order_by(device.c.name).limit(30)
    ).mappings().all()
    in_steps = select(procedure_step.c.procedure_id).where(or_(
        procedure_step.c.title.ilike(pat, escape="\\"), procedure_step.c.body_md.ilike(pat, escape="\\")))
    res["procedures"] = conn.execute(select(procedure).where(or_(
        procedure.c.title.ilike(pat, escape="\\"), procedure.c.intro_md.ilike(pat, escape="\\"),
        procedure.c.id.in_(in_steps))).order_by(procedure.c.title).limit(20)).mappings().all()
    res["runbooks"] = conn.execute(select(runbook).where(or_(
        runbook.c.title.ilike(pat, escape="\\"), runbook.c.body_md.ilike(pat, escape="\\")))
        .order_by(runbook.c.title).limit(20)).mappings().all()
    res["incidents"] = conn.execute(select(incident).where(or_(
        incident.c.title.ilike(pat, escape="\\"), incident.c.cause_md.ilike(pat, escape="\\"),
        incident.c.fix_md.ilike(pat, escape="\\"))).order_by(incident.c.started_at.desc()).limit(20)
    ).mappings().all()
    return res


@router.get("/szukaj")
def search(request: Request, q: str = "", sess: dict = Depends(require_user)):
    q = q.strip()[:100]
    with request.app.state.engine.connect() as conn:
        res = _search(conn, q)
        res["tiles"] = desk.find(conn, q, sess["role"]) if q else []
    return render(request, "szukaj.html", {"q": q, "res": res, "total": sum(len(v) for v in res.values())})


@router.get("/szukaj/szybko")
def quick_search(request: Request, q: str = "", sess: dict = Depends(require_user)):
    """Ctrl+K: krótka lista [{label, sub, url}] — strony panelu, urządzenia, procedury, runbooki, incydenty."""
    q = q.strip()[:100]
    low = q.lower()
    pages = desk.PAGES + (desk.ADMIN_PAGES if sess["role"] == "admin" else [])
    out = [{"label": label, "sub": "strona", "url": url} for label, url in pages if not q or low in label.lower()]
    if q:
        with request.app.state.engine.connect() as conn:
            res = _search(conn, q)
            states = live.saved(conn)
            tiles = desk.find(conn, q, sess["role"])
        out += [{"label": t["name"], "sub": "ikona na pulpicie", "url": t["url"]} for t in tiles[:6]]
        out += [{"label": d["name"], "sub": inv.KINDS.get(d["kind"], d["kind"]) + {"down": " · nie działa", "warn": " · uwaga"}.get(
                     (states.get(d["id"]) or {}).get("state"), ""), "url": f"/urzadzenia/{d['id']}"}
                for d in res["devices"][:8]]
        out += [{"label": p["title"], "sub": "procedura", "url": f"/procedury/{p['id']}"} for p in res["procedures"][:4]]
        out += [{"label": r["title"], "sub": "runbook", "url": f"/runbooki/{r['id']}"} for r in res["runbooks"][:4]]
        out += [{"label": i["title"], "sub": "incydent", "url": f"/incydenty/{i['id']}"} for i in res["incidents"][:4]]
        out += [{"label": f"Szukaj wszędzie: {q}", "sub": "wyniki", "url": f"/szukaj?q={quote(q)}"}]
    return JSONResponse(out[:20])



