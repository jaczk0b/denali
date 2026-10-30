"""Co pokazać na pulpicie: warstwy „Gdzie jest problem” i znaczniki urządzeń z otwartych incydentów.

Stan na żywo przyjdzie z integracjami (Etap 3). Do tego czasu jedynym źródłem są otwarte incydenty,
a reszta jest pokazana uczciwie jako „brak danych”.
"""
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
