"""Mapa infrastruktury rysowana sama z dokumentacji — bez ręcznego układania (zasada „minimum ustawień”).

Kolumny od lewej: internet → sieć → serwery fizyczne → maszyny (VM, LXC, NAS) → aplikacje. W kolumnie urządzenia
są ułożone tak, żeby stały na wysokości tego, na czym działają (mniej krzyżujących się linii). Linie:
- szara: na czym działa (rodzic) i kable / zależność „sieć”,
- niebieska przerywana: zależność „dane” (czerwona, gdy źródło danych nie działa).
Klienci sieci (komputery, telefony, drukarki) domyślnie schowani — jest ich dużo, a rzadko są przyczyną.
"""
from sqlalchemy import select

from .models import cable, dependency, device, device_address

COLUMNS = [
    ("internet", "Internet", {"vps", "modem"}),
    ("siec", "Sieć", {"router", "firewall", "switch", "ap"}),
    ("serwery", "Serwery", {"host", "ups"}),
    ("maszyny", "Maszyny i dane", {"vm", "lxc", "nas"}),
    ("aplikacje", "Aplikacje", {"kontener", "usluga"}),
]
CLIENTS = ("klienci", "Klienci sieci", {"komputer", "telefon", "drukarka", "kamera", "inne"})
NODE_W, NODE_H, GAP_X, GAP_Y, TOP, LEFT = 190, 46, 70, 14, 44, 16


def _column(kind, with_clients):
    for i, (_key, _label, kinds) in enumerate(COLUMNS):
        if kind in kinds:
            return i
    return len(COLUMNS) if with_clients else None


def _state(did, live, marks):
    m = marks.get(did)
    if m == "przyczyna":
        return "down"
    if m in ("skutek", "gorzej"):
        return "effect"
    return (live.get(did) or {}).get("state", "unknown")


def build(conn, live, marks, with_clients=False):
    devs = conn.execute(select(device.c.id, device.c.name, device.c.kind, device.c.parent_id)).mappings().all()
    ips = {}
    for r in conn.execute(select(device_address.c.device_id, device_address.c.value)
                          .where(device_address.c.kind == "ip").order_by(device_address.c.is_primary)):
        ips[r.device_id] = r.value
    cols = COLUMNS + ([CLIENTS] if with_clients else [])
    by_col = {i: [] for i in range(len(cols))}
    for d in devs:
        c = _column(d["kind"], with_clients)
        if c is not None:
            by_col[c].append(d)

    edges = []      # (od, do, rodzaj, twarda)
    for d in devs:
        if d["parent_id"]:
            edges.append((d["id"], d["parent_id"], "hosting", True))
    for r in conn.execute(select(dependency)):
        edges.append((r.device_id, r.depends_on_id, r.type, bool(r.hard)))
    for r in conn.execute(select(cable)):
        edges.append((r.b_device_id, r.a_device_id, "kabel", True))

    # Kolejność w kolumnie: obok tego, z czym się łączy w kolumnie po lewej (średnia pozycja sąsiadów).
    pos, ordered = {}, {}
    neighbours = {}
    for a, b, _k, _h in edges:
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)
    for ci in range(len(cols)):
        items = by_col[ci]

        def key(d):
            near = [pos[n] for n in neighbours.get(d["id"], ()) if n in pos]
            return (sum(near) / len(near) if near else 1e9, d["name"].lower())
        items = sorted(items, key=key)
        ordered[ci] = items
        for j, d in enumerate(items):
            pos[d["id"]] = j
    # drugie przejście od prawej: rodzic bez sąsiadów po lewej (np. serwer) staje przy swoich maszynach
    for ci in range(len(cols) - 2, -1, -1):
        def key2(d):
            near = [pos[n] for n in neighbours.get(d["id"], ()) if n in pos]
            return (sum(near) / len(near) if near else pos[d["id"]], d["name"].lower())
        ordered[ci] = sorted(ordered[ci], key=key2)
        for j, d in enumerate(ordered[ci]):
            pos[d["id"]] = j

    visible = [ci for ci in range(len(cols)) if ordered[ci]]
    nodes, at, columns = [], {}, []
    for x_i, ci in enumerate(visible):
        x = LEFT + x_i * (NODE_W + GAP_X)
        columns.append({"x": x, "label": cols[ci][1]})
        for j, d in enumerate(ordered[ci]):
            y = TOP + j * (NODE_H + GAP_Y)
            at[d["id"]] = (x, y, x_i)
            nodes.append({"id": d["id"], "x": x, "y": y, "name": d["name"], "short": _short(d["name"], 24),
                          "sub": ips.get(d["id"]) or d["kind"], "kind": d["kind"],
                          "state": _state(d["id"], live, marks)})
    states = {n["id"]: n["state"] for n in nodes}
    lines, seen = [], set()
    for a, b, kind, hard in edges:
        if a not in at or b not in at or (a, b, kind) in seen:
            continue
        seen.add((a, b, kind))
        cls = "dane" if kind == "dane" else ""
        if kind == "dane" and states.get(b) == "down":
            cls += " broken"
        if not hard and kind != "dane":
            cls += " soft"
        lines.append({"d": _path(at[a], at[b]), "cls": cls.strip(), "kind": kind})
    lines.sort(key=lambda e: e["kind"] == "dane")      # dane na wierzchu
    height = TOP + max((len(ordered[ci]) for ci in visible), default=0) * (NODE_H + GAP_Y) + 10
    width = LEFT * 2 + len(visible) * NODE_W + max(len(visible) - 1, 0) * GAP_X
    return {"nodes": nodes, "lines": lines, "columns": columns, "width": width, "height": height,
            "w": NODE_W, "h": NODE_H, "hidden_clients": sum(1 for d in devs if _column(d["kind"], True) == len(COLUMNS))}


def _short(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def _path(a, b):
    (ax, ay, ac), (bx, by, bc) = a, b
    ay, by = ay + NODE_H / 2, by + NODE_H / 2
    if ac == bc:            # ta sama kolumna: łuk z prawej strony
        x = ax + NODE_W
        bulge = x + 30 + min(abs(by - ay) / 6, 40)
        return f"M {x} {ay} C {bulge} {ay}, {bulge} {by}, {x} {by}"
    if ac > bc:             # zawsze od lewej do prawej
        (ax, ay, ac), (bx, by, bc) = (bx, by, bc), (ax, ay, ac)
    x1, x2 = ax + NODE_W, bx
    mid = (x1 + x2) / 2
    return f"M {x1} {ay} C {mid} {ay}, {mid} {by}, {x2} {by}"
