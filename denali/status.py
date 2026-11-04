"""Co pokazać na pulpicie: warstwy „Gdzie jest problem” i znaczniki urządzeń z otwartych incydentów.

Stan na żywo przyjdzie z integracjami (Etap 3). Do tego czasu jedynym źródłem są otwarte incydenty,
a reszta jest pokazana uczciwie jako „brak danych”.
"""
import time

from sqlalchemy import select

from .models import device, incident, incident_device, incident_event

# Od góry to, czego używasz, na dole to, na czym stoi.
LAYERS = [
    ("aplikacje", "Aplikacje", {"usluga", "kontener"}),
    ("dane", "Dane", {"nas"}),
    ("serwery", "Serwery", {"host", "vm", "lxc"}),
    ("siec", "Sieć", {"router", "switch", "ap", "firewall", "modem"}),
    ("internet", "Internet", {"vps"}),
]
_RANK = {"przyczyna": 0, "skutek": 1, "gorzej": 2}


def layer_of(kind):
    for key, _label, kinds in LAYERS:
        if kind in kinds:
            return key
    return "inne"


def open_incidents(conn):
    return conn.execute(select(incident).where(incident.c.status != "zamkniety")
                        .order_by(incident.c.started_at.desc())).mappings().all()


def device_marks(conn):
    """{device_id: przyczyna | skutek | gorzej} z otwartych incydentów (najważniejsza rola wygrywa)."""
    rows = conn.execute(select(incident_device.c.device_id, incident_device.c.role)
                        .join(incident, incident.c.id == incident_device.c.incident_id)
                        .where(incident.c.status != "zamkniety")).all()
    marks = {}
    for r in rows:
        if r.device_id not in marks or _RANK[r.role] < _RANK[marks[r.device_id]]:
            marks[r.device_id] = r.role
    return marks


def layers(devices, marks, live=None):
    """live: {device_id: {"state", "why"}} ze stanu liczonego przez workera (puste = brak danych)."""
    live = live or {}
    out = []
    for key, label, _kinds in LAYERS:
        devs = [d for d in devices if layer_of(d["kind"]) == key]
        causes = [d for d in devs if marks.get(d["id"]) == "przyczyna"]
        effects = [d for d in devs if marks.get(d["id"]) in ("skutek", "gorzej")]
        known = [d for d in devs if live.get(d["id"], {}).get("state") in ("ok", "warn", "down", "off")]
        warn = [d for d in devs if live.get(d["id"], {}).get("state") == "warn"]
        ok = [d for d in devs if live.get(d["id"], {}).get("state") == "ok"]
        title = label
        if causes:
            state, text = "down", "tu jest przyczyna"
            title = f"{label} · " + ", ".join(d["name"] for d in causes)
        elif effects:
            state, text = "effect", f"{len(effects)} z {len(devs)} — skutek, nie osobna awaria"
        elif warn:
            state, text = "warn", "uwaga: " + ", ".join(d["name"] for d in warn)
        elif known:
            rest = len(devs) - len(known)
            state = "ok"
            text = f"{len(ok)} z {len(devs)} działa" + (f" · {rest} bez danych" if rest else "")
            off = len(known) - len(ok)
            if off:
                text += f" · {off} wyłączone"
        elif devs:
            state, text = "unknown", f"{len(devs)} urządz. · brak danych o stanie"
        else:
            state, text = "none", "nic tu nie ma"
        out.append({"key": key, "label": label, "title": title, "state": state, "text": text, "count": len(devs),
                    "link": (causes or effects or [None])[0]})
    return out


def _devices_word(n):
    """1 urządzenie, 2–4 urządzenia, 5+ urządzeń (12–14 też „urządzeń”)."""
    if n == 1:
        return "urządzenie"
    return "urządzenia" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "urządzeń"


def verdict(devices, states, open_inc, breaks, last_closed=None, now=None):
    """Jedno zdanie na górę pulpitu: czy wszystko działa, a jeśli nie — co i od kiedy.

    states: stan na żywo, open_inc: otwarte incydenty (najnowsze pierwsze), breaks: {device_id: przerwa},
    last_closed: ostatni zamknięty incydent (do „spokojnie od …”)."""
    names = {d["id"]: d["name"] for d in devices}
    pauses = [f"{names.get(d, d)} do {time.strftime('%H:%M', time.localtime(m['ends_at']))}"
              for d, m in breaks.items() if d in names]
    out = {"pauses": pauses, "items": []}
    if open_inc:
        n = len(open_inc)
        first = open_inc[-1]                    # najstarszy — od niego zwykle wszystko się zaczęło
        word = "problem" if n == 1 else ("problemy" if n < 5 else "problemów")
        out.update(state="down" if any(i["severity"] == "krytyczny" for i in open_inc) else "warn",
                   headline=f"{n} {word}: {first['title']}",
                   sub=(states.get(first["root_device_id"]) or {}).get("why", ""),
                   since=first["started_at"], link=f"/incydenty/{first['id']}",
                   items=[{"title": i["title"], "since": i["started_at"], "link": f"/incydenty/{i['id']}",
                           "acked": i["status"] == "potwierdzony"} for i in open_inc[:-1][:3]])
        return out
    if not states:
        out.update(state="unknown", headline="Nie wiem, co działa",
                   sub="Brak stanu na żywo — podłącz integrację albo sprawdź, czy działa worker.")
        return out
    waiting = sorted(((v["pending_until"], names[d]) for d, v in states.items() if v.get("pending_until") and d in names))
    if waiting:
        until = time.strftime("%H:%M", time.localtime(waiting[0][0]))
        who = ", ".join(n for _, n in waiting[:3]) + (" i inne" if len(waiting) > 3 else "")
        out.update(state="warn", headline=f"Nie odpowiada: {who}",
                   sub=f"jeszcze nie awaria — jeśli nie wróci do {until}, otworzę incydent")
        return out
    warn = [names[d] for d, v in states.items() if v["state"] == "warn" and d in names]
    checked = sum(1 for v in states.values() if v["state"] in ("ok", "warn"))
    if warn:
        out.update(state="warn", headline="Działa, ale " + ("jest uwaga" if len(warn) == 1 else f"są {len(warn)} uwagi"),
                   sub=", ".join(sorted(warn)[:4]) + (" i inne" if len(warn) > 4 else ""))
        return out
    calm = ""
    if last_closed and last_closed.get("ended_at"):
        days = int(((now or time.time()) - last_closed["ended_at"]) // 86400)
        calm = " · bez awarii od " + ("dziś" if days == 0 else ("wczoraj" if days == 1 else f"{days} dni"))
    out.update(state="ok", headline="Wszystko działa", sub=f"sprawdzono {checked} {_devices_word(checked)}{calm}")
    return out


def recent_events(conn, limit=5):
    return conn.execute(select(incident_event.c.ts, incident_event.c.text, incident_event.c.incident_id)
                        .order_by(incident_event.c.ts.desc(), incident_event.c.id.desc()).limit(limit)).mappings().all()


def pulpit_apps(conn):
    return conn.execute(select(device).where(device.c.pulpit_group != "brak").order_by(device.c.name)
                        ).mappings().all()


def tile_color(name):
    """Stały kolor kafla aplikacji wyliczony z nazwy (6 barw z makiety) — bez ustawiania czegokolwiek."""
    return f"c{sum(ord(ch) for ch in (name or '').lower()) % 6}"


def initials(name):
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()
