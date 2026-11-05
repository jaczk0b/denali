"""Dane nowego pulpitu: baner awarii, Stan labu, kafelki ze stanem, Statystyki i panel boczny.

Tylko odczyt, wszystko z tego, co panel już ma. Czego nie ma, pokazujemy jako „brak danych”, a nie jako zero.
Układ (kolejność, widoczność) trzyma desk.py.
"""
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from . import alerts, changes, desk, infra_views, inventory
from .models import check_state, device, incident_device, incident_event, integration

log = logging.getLogger(__name__)

MACHINE_KINDS = {"host", "vm", "lxc", "nas"}
MAX_CHAIN_UP = 3            # tyle poziomów „na czym to stoi” pokazujemy nad przyczyną
MAX_EFFECTS = 2             # więcej skutków zwijamy do jednego wiersza
MAX_ATTENTION = 6
EVENT_HOURS = 48
CERT_WARN_DAYS = 30


def ago(seconds):
    """Krótko: 12 min, 3 h, 5 dni."""
    seconds = max(0, seconds)
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h"
    days = int(seconds // 86400)
    return "1 dzień" if days == 1 else f"{days} dni"


def _duration(seconds):
    """Czas trwania na banerze: 6 h 56 min."""
    if seconds < 3600:
        return ago(seconds)
    hours, minutes = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{hours} h {minutes} min" if hours < 48 else ago(seconds)


def muted(inc, now):
    return bool(inc["muted_until"] and inc["muted_until"] > now)


def mute_options(now):
    """[(klucz, napis, kiedy)] przycisków „Wycisz do…”. Pierwszy jest główny."""
    moment = datetime.fromtimestamp(now)
    morning = (moment + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return [("rano", "jutra 8:00", morning.timestamp()), ("1h", "za godzinę", now + 3600), ("4h", "za 4 godziny", now + 4 * 3600)]


def banner(conn, open_inc, states, names, now=None):
    """Najważniejsza awaria na górę pulpitu. Wyciszone schodzą na bok: same zwijają baner do jednej linii."""
    if not open_inc:
        return None
    now = now or time.time()
    awake = [i for i in open_inc if not muted(i, now)]
    pool = awake or open_inc
    main = sorted(pool, key=lambda i: (i["severity"] != "krytyczny", i["started_at"]))[0]
    root = main["root_device_id"]
    why = (states.get(root) or {}).get("why", "")
    cause = (main["cause_md"] or "").strip().split("\n")[0] or why
    events = conn.execute(select(incident_event.c.kind, incident_event.c.text, incident_event.c.user)
                          .where(incident_event.c.incident_id == main["id"], incident_event.c.kind.in_(("notatka", "zmiana")))
                          .order_by(incident_event.c.id.desc())).all()
    note = next((e for e in events if e.kind == "notatka"), None)
    change = next((e.text for e in events if e.kind == "zmiana"), "")
    hit = [r.device_id for r in conn.execute(select(incident_device.c.device_id).where(
        incident_device.c.incident_id == main["id"], incident_device.c.role.in_(("skutek", "gorzej"))))]
    return {"id": main["id"], "title": main["title"], "state": "down" if main["severity"] == "krytyczny" else "warn",
            "acked": main["status"] == "potwierdzony", "acked_by": main["acked_by"], "since": main["started_at"],
            "lasted": _duration(now - main["started_at"]), "cause": cause, "why": why if why != cause else "",
            "root": names.get(root, ""), "root_id": root, "hit": len(hit), "hit_names": [names[d] for d in hit if d in names][:4],
            "change": change, "note": note.text if note else "", "note_by": note.user if note else "",
            "collapsed": not awake, "muted_until": main["muted_until"] if muted(main, now) else None,
            "others": [{"id": i["id"], "title": i["title"], "since": i["started_at"], "muted_until": i["muted_until"] if muted(i, now) else None}
                       for i in open_inc if i["id"] != main["id"]][:3],
            "options": mute_options(now)}


# ───────────────────────── Stan labu ─────────────────────────
def _kpi(label, value, unit, state, note):
    return {"label": label, "value": value, "unit": unit, "state": state, "note": note}


def _word(n):
    return "ostrzeżeniem" if n == 1 else "ostrzeżeniami"


def _kpi_apps(devices, states, checks):
    apps = [d for d in devices if d["app_url"]]
    if not apps:
        return _kpi("Aplikacje w domu", "—", "", "unknown", "brak aplikacji z adresem")
    known = [d for d in apps if d["id"] in states]
    up = [d for d in known if states[d["id"]]["state"] in ("ok", "warn")]
    outside = [c for c in checks if c["kind"] == "internet"]
    if outside:
        good = sum(1 for c in outside if c["ok"])
        return _kpi("Aplikacje w domu", len(up), f"/{len(apps)}", "ok" if good == len(outside) else "down", f"{good}/{len(outside)} z zewnątrz")
    return _kpi("Aplikacje w domu", len(up), f"/{len(apps)}", "ok" if len(up) == len(apps) else "warn",
                "wszystkie działają" if len(up) == len(apps) else f"{len(apps) - len(known)} bez danych" if len(known) < len(apps) else "część nie działa")


def _kpi_machines(devices, states):
    all_ = [d for d in devices if d["kind"] in MACHINE_KINDS]
    if not all_:
        return _kpi("Serwery i maszyny", "—", "", "unknown", "brak maszyn")
    now_up = [d for d in all_ if states.get(d["id"], {}).get("state") in ("ok", "warn")]
    warn = sum(1 for d in all_ if states.get(d["id"], {}).get("state") == "warn")
    down = sum(1 for d in all_ if states.get(d["id"], {}).get("state") == "down")
    if down:
        return _kpi("Serwery i maszyny", len(now_up), f"/{len(all_)}", "down", f"{down} nie działa")
    if warn:
        return _kpi("Serwery i maszyny", len(now_up), f"/{len(all_)}", "warn", f"{warn} z {_word(warn)}")
    return _kpi("Serwery i maszyny", len(now_up), f"/{len(all_)}", "ok" if now_up else "unknown", "bez ostrzeżeń" if now_up else "brak danych")


def _kpi_copy(truenas, now):
    tasks = [t for src in truenas for t in src["tasks"] if t["last"]]
    tasks = [t for t in tasks if t["kind"] == "replikacja"] or tasks
    if not tasks:
        return _kpi("Ostatnia kopia NAS", "—", "", "unknown", "brak danych")
    oldest = min(tasks, key=lambda t: t["last"])
    age = now - oldest["last"]
    days = int(age // 86400)
    value, unit = (days, " dni" if days != 1 else " dzień") if days else (int(age // 3600), " h")
    state = "warn" if oldest["state"] in ("warn", "down") else "ok"
    return _kpi("Ostatnia kopia NAS", value, unit, state, f"{oldest['kind']} {oldest['key']}")


def _bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return (f"{n:.0f}" if n >= 100 else f"{n:.1f}").replace(".", ",") + f" {unit}"
        n /= 1024


def _kpi_space(truenas):
    pools = [p for src in truenas for p in src["pools"] if isinstance(p["used"], (int, float))]
    if not pools:
        return _kpi("Miejsce na NAS", "—", "", "unknown", "brak danych")
    full = max(pools, key=lambda p: p["used"])
    note = f"{_bytes(full['allocated'])} z {_bytes(full['size'])}" if full.get("allocated") and full.get("size") else f"pula {full['name']}"
    return _kpi("Miejsce na NAS", round(full["used"] * 100), "%", "warn" if full["used"] >= 0.9 else "ok", note)


# ───────────────────────── kafelki ─────────────────────────
def _host(url):
    return urlsplit(url).netloc or url


def tile_status(tile, marks, states):
    """(stan, podpis) kafelka albo None, gdy skrót nie ma urządzenia."""
    did = tile["device_id"]
    if not did:
        return None
    mark = marks.get(did)
    if mark:
        return {"przyczyna": ("down", "Nie działa"), "skutek": ("effect", "Bez danych"), "gorzej": ("warn", "Działa gorzej")}[mark]
    s = states.get(did)
    if not s:
        return "unknown", "Brak danych"
    if s["state"] == "ok":
        return "ok", "Działa"
    if s["state"] == "off":
        return "unknown", "Wyłączone"
    return ("warn" if s["state"] == "warn" else "down"), s.get("why") or ("Uwaga" if s["state"] == "warn" else "Nie działa")


def decorate(board, marks, states, checks):
    """Dopisuje do kafelków stan, adres do pokazania i „tylko w domu”: z internetu nie działa, w domu tak."""
    failing = {c["device_id"] for c in checks if c["kind"] == "internet" and not c["ok"]}
    for cat in board:
        for t in cat["tiles"]:
            did = t["device_id"]
            # Skutek awarii zdalnego dostępu w domu działa normalnie: liczy się stan z LAN, a nie „bez danych”.
            lan_only = did in failing and states.get(did, {}).get("state") in ("ok", "warn")
            status = tile_status(t, {} if lan_only else marks, states)
            t["status"], t["status_text"] = status or (None, "")
            t["mark"] = None if lan_only else marks.get(did)
            t["host"] = _host(t["url"])
            t["lan_only"] = lan_only
    return board


# ───────────────────────── Statystyki ─────────────────────────
def _on(cfg, key):
    """Bez wyboru metryk (widżety startowe) pokazujemy wszystko."""
    chosen = cfg.get("metrics")
    return not chosen or key in chosen


def _limits(cfg, key):
    warn, crit = (cfg.get("warn") or {}).get(key), (cfg.get("crit") or {}).get(key)
    return warn, crit


def _row(key, label, value, state=""):
    return {"key": key, "label": label, "value": value, "state": state}


def _bar(cfg, key, label, value, fraction):
    pct = max(0, min(100, round((fraction or 0) * 100, 1)))
    warn, crit = _limits(cfg, key)
    level = "crit" if crit and pct >= crit else "hi" if (warn and pct >= warn) or (not warn and pct >= 90) else ""
    return {"key": key, "label": label, "value": value, "pct": pct, "level": level, "tick": warn, "warn": warn, "crit": crit}


def _missing(source):
    return {"empty": True, "source": source}


def _pick(pairs, cfg):
    """Wskazany obiekt albo domyślny: [(ref, cokolwiek)], ref = „integracja:obiekt”."""
    return next((p for ref, p in pairs if ref == cfg.get("ref")), None)


def _proxmox(conn, _now, cfg):
    rows = infra_views.proxmox(conn)
    nodes = [(f"{src['row']['id']}:{n['eid']}", (src, n)) for src in rows for n in src["nodes"]]
    if not nodes:
        return _missing("Proxmox")
    src, n = _pick(nodes, cfg) or next((p for _r, p in nodes if p[1]["maxmem"]), nodes[0][1])
    bars = []
    if _on(cfg, "cpu") and isinstance(n["cpu"], (int, float)):
        bars.append(_bar(cfg, "cpu", "CPU", f"{n['cpu'] * 100:.0f}%", n["cpu"]))
    if _on(cfg, "ram") and n["maxmem"]:
        bars.append(_bar(cfg, "ram", "RAM", f"{_bytes(n['mem'] or 0)} z {_bytes(n['maxmem'])}", (n["mem"] or 0) / n["maxmem"]))
    foot = [f"{n['running']} z {n['guests']} VM i LXC działa"] if _on(cfg, "maszyny") and n["guests"] else []
    if _on(cfg, "czas") and isinstance(n["uptime"], (int, float)) and n["uptime"] > 0:
        foot.append("działa " + ago(n["uptime"]))
    return {"title": n["name"], "source": src["row"]["name"], "state": n["state"], "bars": bars, "foot": " · ".join(foot),
            "device_id": n["device_id"], "integration_id": src["row"]["id"], "external_id": n["eid"]}


def _pool(conn, _now, cfg):
    sources = infra_views.truenas(conn)
    pools = [(f"{src['row']['id']}:pool/{p['name']}", (src, p)) for src in sources for p in src["pools"]]
    if not pools:
        return _missing("TrueNAS")
    src, p = _pick(pools, cfg) or max((x for _r, x in pools), key=lambda x: x[1]["used"] or 0)
    bars = []
    if _on(cfg, "zajete") and p.get("allocated") and p.get("size"):
        bars.append(_bar(cfg, "zajete", "Zajęte", f"{_bytes(p['allocated'])} z {_bytes(p['size'])}", p["used"]))
    disks = src["disks"]
    bad = sum(1 for d in disks if d.get("problems"))
    temps = [d["temp"] for d in disks if isinstance(d.get("temp"), (int, float))]
    rows = []
    if _on(cfg, "stan"):
        rows.append(_row("stan", "Stan puli", {"ok": "Działa", "warn": "Uwaga", "down": "Nie działa"}.get(p["state"], "Brak danych") + (f" · {p['status']}" if p["status"] else ""), p["state"]))
    if _on(cfg, "dyski") and disks:
        rows.append(_row("dyski", "Dyski z problemem", f"{bad} z {len(disks)}", "warn" if bad else ""))
    if _on(cfg, "temp") and temps:
        rows.append(_row("temp", "Temperatury", f"{min(temps):.0f}–{max(temps):.0f} °C"))
    return {"title": p["name"], "source": src["row"]["name"], "state": p["state"], "bars": bars, "rows": rows,
            "device_id": src["device_id"], "integration_id": src["row"]["id"], "external_id": f"pool/{p['name']}"}


def _backups(conn, now, cfg):
    items, sources = [], []
    if _on(cfg, "zadania"):
        for src in infra_views.truenas(conn):
            if src["tasks"]:
                sources.append(src["row"]["name"])
            for t in src["tasks"]:
                when = f"{ago(now - t['last'])} temu" if t["last"] else "bez danych"
                items.append({"state": t["state"], "name": f"{t['kind'].capitalize()} {t['key']}", "when": when, "ts": t["last"] or 0})
    if _on(cfg, "maszyny"):
        for src in infra_views.sources(conn, "pbs"):
            rows = [s for eid, s in src["states"].items() if eid.startswith("backup/")]
            if rows:
                sources.append(src["row"]["name"])
                late = sum(1 for s in rows if s["state"] != "ok")
                items.append({"state": "warn" if late else "ok", "name": f"Kopie maszyn ({len(rows)})",
                              "when": f"{late} spóźnione" if late else "na czas", "ts": 0})
    if not items:
        return _missing("TrueNAS, PBS")
    items.sort(key=lambda x: (x["state"] == "ok", -x["ts"]))
    return {"source": ", ".join(sources), "state": "warn" if any(i["state"] != "ok" for i in items) else "ok", "entries": items[:4]}


def _containers(conn, _now, cfg):
    rows = infra_views.docker(conn)
    if not rows:
        return _missing("Komodo")
    stacks = sum(r["count"]["stacks"] for r in rows)
    bad = sum(r["count"]["problem"] for r in rows)
    fresh = sum(r["count"]["updates"] for r in rows)
    named = [s["name"] for r in rows for g in r["groups"] for s in g["stacks"] if s["updates"]][:2]
    out = []
    if _on(cfg, "problemy"):
        out.append(_row("problemy", "Z problemem", str(bad), "warn" if bad else ""))
    if _on(cfg, "obrazy"):
        out.append(_row("obrazy", "Nowe obrazy", f"{fresh}" + (f" · {', '.join(named)}" if named else "")))
    data = {"source": ", ".join(r["row"]["name"] for r in rows), "state": "warn" if bad else "ok", "rows": out}
    if _on(cfg, "liczba"):
        data.update(big=stacks - bad, unit=f"/{stacks}", big_note="stacków działa")
    return data


def _network(conn, _now, cfg):
    rows = infra_views.network(conn)
    if not rows:
        return _missing("OpenWrt, TP-Link")
    router = rows[0]["router"]
    out = []
    if _on(cfg, "urzadzenia"):
        out.append(_row("urzadzenia", "Urządzenia w sieci", str(sum(r["count"]["all"] for r in rows))))
    if _on(cfg, "wifi"):
        out.append(_row("wifi", "Wi-Fi / przewód", f"{sum(r['count']['wifi'] for r in rows)} / {sum(r['count']['wired'] for r in rows)}"))
    new = sum(r["count"]["new"] for r in rows)
    weak = sum(r["count"]["weak"] for r in rows)
    if new and _on(cfg, "nowe"):
        out.append(_row("nowe", "Nowe, do zatwierdzenia", str(new), "warn"))
    if weak and _on(cfg, "slaby"):
        out.append(_row("slaby", "Słaby sygnał", str(weak), "warn"))
    return {"source": ", ".join(r["row"]["name"] for r in rows), "state": router["state"] if router else "unknown", "rows": out,
            "foot": router["summary"] if router and _on(cfg, "router") else ""}


def _certs(conn, now, cfg):
    rows = conn.execute(select(check_state.c.target, check_state.c.cert_expires_at).where(check_state.c.cert_expires_at.isnot(None))).all()
    certs = {}
    for r in rows:
        host = r.target.split("/")[2] if "//" in r.target else r.target
        certs[host] = min(certs.get(host, r.cert_expires_at), r.cert_expires_at)
    if not certs:
        return _missing("sprawdzenia HTTP")
    valid = sum(1 for at in certs.values() if at > now)
    host, at = min(certs.items(), key=lambda x: x[1])
    left = (at - now) / 86400
    state = "down" if left < 7 else "warn" if left < CERT_WARN_DAYS else "ok"
    text = f"wygasł {ago(now - at)} temu" if left < 0 else f"za {round(left)} dni"
    data = {"source": "sprawdzenia HTTP", "state": state}
    if _on(cfg, "liczba"):
        data.update(big=valid, unit="", big_note="ważnych")
    if _on(cfg, "najblizszy"):
        data["next"] = {"host": host, "when": text, "state": state}
    return data


WIDGET_DATA = {"proxmox": _proxmox, "pula": _pool, "kopie": _backups, "kontenery": _containers, "siec": _network, "certyfikaty": _certs}


def widget(conn, row, now=None):
    """Dane jednego widżetu. Awaria źródła nie może zabrać pulpitu, więc widżet mówi wtedy, czego mu brakuje."""
    now = now or time.time()
    cfg = row["config"] or {}
    label = desk.WIDGETS.get(row["kind"], (row["kind"],))[0]
    try:
        data = WIDGET_DATA[row["kind"]](conn, now, cfg)
    except Exception:  # noqa: BLE001
        log.exception("widżet %s", row["kind"])
        data = _missing(label)
    return {"id": row["id"], "kind": row["kind"], "title": row["title"], "size": row["size"], "config": cfg, "label": label,
            "view": cfg.get("view", "paski"), **{k: v for k, v in data.items() if k != "title"},
            "name": row["title"] or data.get("title") or label}


def widgets(conn, rows, now=None):
    return [widget(conn, r, now) for r in rows]


def breaches(conn, now=None):
    """Przekroczone progi widżetów z włączonym ostrzeganiem: trafiają do zwykłych alertów (alerts.collect)."""
    out = []
    for w in widgets(conn, [r for r in desk.widgets(conn) if (r["config"] or {}).get("alert")], now):
        for b in w.get("bars") or []:
            over = "crit" if b["crit"] and b["pct"] >= b["crit"] else "warn" if b["warn"] and b["pct"] >= b["warn"] else ""
            if over:
                out.append({"key": f"{w['id']}:{b['key']}", "device_id": w.get("device_id"), "integration_id": w.get("integration_id"),
                            "external_id": w.get("external_id"), "severity": alerts.CRIT if over == "crit" else alerts.WARN,
                            "text": f"{w['name']}: {b['label']} {b['value']} (próg {b[over]}%)"})
    return out


def _refs(conn):
    """Co da się wybrać w źródle, gdy obiektów jest kilka: węzły Proxmoxa i pule."""
    return {"proxmox": [{"ref": f"{s['row']['id']}:{n['eid']}", "label": f"{s['row']['name']} · {n['name']}"} for s in infra_views.proxmox(conn) for n in s["nodes"]],
            "pula": [{"ref": f"{s['row']['id']}:pool/{p['name']}", "label": f"{s['row']['name']} · {p['name']}"} for s in infra_views.truenas(conn) for p in s["pools"]]}


def stat_sources(conn):
    """Dane okna „Nowa statystyka”: źródła, ich metryki, domyślne progi i to, czego na razie nie ma."""
    used = {r.type for r in conn.execute(select(integration.c.type))}
    refs = _refs(conn)
    out = []
    for kind, (label, _source, _what) in desk.WIDGETS.items():
        need = desk.WIDGET_SOURCES.get(kind)
        metrics = [{"key": k, "label": name, "note": note, "limit": desk.THRESHOLDS.get(k)} for k, name, note in desk.WIDGET_METRICS[kind]]
        out.append({"kind": kind, "label": label, "connected": need is None or bool(need & used), "metrics": metrics,
                    "missing": desk.WIDGET_MISSING.get(kind, ()), "refs": refs.get(kind, [])})
    return out


# ───────────────────────── panel boczny ─────────────────────────
def attention(conn, open_inc, devices, states, now=None):
    """(wiersze, ile pozostałych działa). Każde ostrzeżenie ma powód, a nie sam kolor."""
    now = now or time.time()
    names = {d["id"]: d["name"] for d in devices}
    rows = []
    for i in sorted(open_inc, key=lambda x: x["started_at"]):
        detail = (states.get(i["root_device_id"]) or {}).get("why") or "Otwarty incydent"
        if i["status"] == "potwierdzony":
            detail = f"Potwierdzone: {i['acked_by']}" if i["acked_by"] else "Potwierdzone"
        rows.append({"state": "down" if i["severity"] == "krytyczny" else "warn", "title": i["title"], "detail": detail,
                     "when": ago(now - i["started_at"]), "link": f"/incydenty/{i['id']}"})
    covered = set()
    for a in alerts.active(conn, now=now):
        if a["silenced"]:
            continue
        covered.add(a["device_id"])
        rows.append({"state": "down" if a["severity"] == alerts.CRIT else "warn", "title": a["device"] or a["label"], "detail": a["message"],
                     "when": ago(now - a["first_seen"]), "link": f"/urzadzenia/{a['device_id']}" if a["device_id"] else "/alerty"})
    for did, s in states.items():
        if s["state"] == "warn" and did not in covered and did in names:
            rows.append({"state": "warn", "title": names[did], "detail": s.get("why") or "Stan: uwaga", "when": "", "link": f"/urzadzenia/{did}"})
    good = sum(1 for d in devices if states.get(d["id"], {}).get("state") == "ok")
    return rows[:MAX_ATTENTION], len(rows) - min(len(rows), MAX_ATTENTION), good


def _step(state, name, detail, cause=False, link=""):
    return {"state": state, "name": name, "detail": detail, "cause": cause, "link": link}


def chain(conn, main, states, names):
    """Skąd ta awaria: od tego, na czym stoi przyczyna, przez przyczynę, do skutków."""
    if not main or not main["root_id"]:
        return []
    root = main["root_id"]
    graph = inventory.load_graph(conn)
    path, node, seen = [], root, {root}
    while len(path) < MAX_CHAIN_UP:
        ups = [e.parent for e in graph.up.get(node, []) if e.hard and e.parent not in seen]
        if not ups:
            break
        node = ups[0]
        seen.add(node)
        path.append(node)
    steps = []
    for d in reversed(path):
        s = states.get(d) or {}
        state = {"ok": "ok", "warn": "warn", "down": "down"}.get(s.get("state"), "unknown")
        steps.append(_step(state, names.get(d, str(d)), s.get("why") or ("Działa" if state == "ok" else "Brak danych"), link=f"/urzadzenia/{d}"))
    steps.append(_step(main["state"], names.get(root, main["title"]), main["cause"] or "Tu jest przyczyna", True, f"/urzadzenia/{root}"))
    hit = main["hit_names"]
    if main["hit"] > MAX_EFFECTS:
        steps.append(_step("effect", f"{main['hit']} urządzeń dotkniętych", ", ".join(hit) + (" i inne" if main["hit"] > len(hit) else "")))
    else:
        steps += [_step("effect", n, "Skutek, nie osobna awaria") for n in hit]
    return steps


def events(conn, now=None, limit=5):
    now = now or time.time()
    today = datetime.fromtimestamp(now).date()
    out = []
    for e in changes.timeline(conn, now - EVENT_HOURS * 3600, limit=limit):
        day = datetime.fromtimestamp(e["ts"]).date()
        out.append({"when": datetime.fromtimestamp(e["ts"]).strftime("%H:%M") if day == today else "wcz." if (today - day).days == 1 else day.strftime("%d.%m"),
                    "text": e["text"], "detail": e["device"] or e["label"], "hot": e["kind"] == "incydent",
                    "link": f"/urzadzenia/{e['device_id']}" if e["device_id"] else "/os-czasu"})
    return out


def kpis(conn, devices, states, checks, now=None):
    now = now or time.time()
    truenas = infra_views.truenas(conn)
    return [_kpi_apps(devices, states, checks), _kpi_machines(devices, states), _kpi_copy(truenas, now), _kpi_space(truenas)]


def load_checks(conn):
    return conn.execute(select(check_state)).mappings().all()


def device_rows(conn):
    return conn.execute(select(device.c.id, device.c.name, device.c.kind, device.c.app_url)).mappings().all()
