"""Mapa infrastruktury rysowana sama z dokumentacji — bez ręcznego układania (zasada „minimum ustawień”).

Układ jak w makiecie: od lewej internet → sieć → serwery fizyczne jako ramki z maszynami w środku (VM, LXC, NAS)
→ aplikacje (stacki, usługi) obok maszyny, na której działają. To, co w czymś działa, stoi w ramce — bez linii
do serwera. Linie:
- szara: sieć i kable (do ramki serwera, nie do każdej maszyny),
- niebieska przerywana: zależność „dane” (czerwona, gdy źródło danych nie działa),
- cienka szara: aplikacja → maszyna, na której działa.
Klienci sieci (komputery, telefony) domyślnie schowani; po włączeniu stoją pod routerem / AP.
"""
from sqlalchemy import select

from .models import cable, dependency, device, device_address

INTERNET = {"vps", "modem"}
NETWORK = {"router", "firewall", "switch", "ap"}
HOSTS = {"host"}
MACHINES = {"vm", "lxc", "nas", "ups"}
APPS = {"kontener", "usluga"}
CLIENTS = {"komputer", "telefon", "drukarka", "kamera", "inne"}

W, H, GAP_X, GAP_Y, TOP, LEFT = 200, 48, 64, 12, 40, 16
BOX_PAD, BOX_HEAD, BOX_GAP = 14, 30, 22
STATE_LABEL = {"ok": "działa", "down": "nie działa", "warn": "uwaga", "off": "wyłączone", "effect": "skutek awarii",
               "unknown": "brak danych"}


def _state(did, live, marks):
    m = marks.get(did)
    if m == "przyczyna":
        return "down"
    if m in ("skutek", "gorzej"):
        return "effect"
    return (live.get(did) or {}).get("state", "unknown")


def build(conn, live, marks, with_clients=False):
    devs = {d["id"]: dict(d) for d in conn.execute(
        select(device.c.id, device.c.name, device.c.kind, device.c.parent_id).order_by(device.c.name)).mappings()}
    ips = {}
    for r in conn.execute(select(device_address.c.device_id, device_address.c.value)
                          .where(device_address.c.kind == "ip").order_by(device_address.c.is_primary)):
        ips[r.device_id] = r.value
    edges = [(r.device_id, r.depends_on_id, r.type, bool(r.hard)) for r in conn.execute(select(dependency))]
    edges += [(r.b_device_id, r.a_device_id, "kabel", True) for r in conn.execute(select(cable))]

    def kind(d):
        return d["kind"]

    hosts = [d for d in devs.values() if kind(d) in HOSTS]
    host_ids = {d["id"] for d in hosts}
    inside = {h["id"]: [] for h in hosts}           # maszyny w ramce serwera
    loose = []                                      # maszyny bez serwera (np. fizyczny NAS)
    for d in devs.values():
        if kind(d) in MACHINES:
            (inside[d["parent_id"]] if d["parent_id"] in host_ids else loose).append(d)
    apps = [d for d in devs.values() if kind(d) in APPS]
    nodes, rect = [], {}                            # rect: device_id → (x, y, w, h)

    def node(d, x, y, sub=None):
        rect[d["id"]] = (x, y, W, H)
        s = _state(d["id"], live, marks)
        nodes.append({"id": d["id"], "x": x, "y": y, "name": d["name"], "short": _short(d["name"], 24),
                      "sub": sub or ips.get(d["id"]) or "", "kind": kind(d), "state": s, "label": STATE_LABEL.get(s, s)})

    columns, x = [], LEFT
    # 1–2: internet i sieć (z klientami pod spodem, gdy włączeni)
    for label, kinds in (("Internet", INTERNET), ("Sieć", NETWORK)):
        col = [d for d in devs.values() if kind(d) in kinds]
        if label == "Sieć" and with_clients:
            col += [d for d in devs.values() if kind(d) in CLIENTS]
        if not col:
            continue
        columns.append({"x": x, "label": label})
        for j, d in enumerate(col):
            node(d, x, TOP + j * (H + GAP_Y))
        x += W + GAP_X
    # 3: serwery jako ramki z maszynami; maszyny bez serwera pod ramkami
    boxes, y = [], TOP
    servers_x = x
    if hosts or loose:
        columns.append({"x": x, "label": "Serwery i maszyny"})
    for h in sorted(hosts, key=lambda h: (-len(inside[h["id"]]), h["name"].lower())):
        # maszyny z aplikacjami na koniec — w ramce z dwiema kolumnami trafiają do prawej, bliżej kolumny aplikacji
        has_apps = {d["parent_id"] for d in apps}
        kids = sorted(inside[h["id"]], key=lambda d: (d["id"] in has_apps, d["name"].lower()))
        if not kids:
            node(h, x, y)
            y += H + BOX_GAP
            continue
        per_col = len(kids) if len(kids) <= 8 else (len(kids) + 1) // 2
        ncols = 1 if len(kids) <= 8 else 2
        bw = BOX_PAD * 2 + ncols * W + (ncols - 1) * BOX_PAD
        bh = BOX_HEAD + BOX_PAD + per_col * (H + GAP_Y) - GAP_Y + BOX_PAD
        s = _state(h["id"], live, marks)
        boxes.append({"id": h["id"], "x": x, "y": y, "w": bw, "h": bh, "name": h["name"], "ip": ips.get(h["id"], ""),
                      "state": s, "label": STATE_LABEL.get(s, s), "count": len(kids)})
        rect[h["id"]] = (x, y, bw, bh)
        for i, d in enumerate(kids):
            c, r = divmod(i, per_col)
            node(d, x + BOX_PAD + c * (W + BOX_PAD), y + BOX_HEAD + BOX_PAD + r * (H + GAP_Y))
        y += bh + BOX_GAP
    for d in loose:
        node(d, x, y)
        y += H + GAP_Y
    servers_w = max([b["w"] for b in boxes] + ([W] if hosts or loose else [0]))
    x = servers_x + servers_w + (GAP_X if servers_w else 0)
    # 4: aplikacje na wysokości maszyny, na której działają
    if apps:
        columns.append({"x": x, "label": "Aplikacje"})
        def at(d):
            r = rect.get(d["parent_id"])
            return (r[1] if r else 1e9, d["name"].lower())
        y = TOP
        for d in sorted(apps, key=at):
            want = rect[d["parent_id"]][1] if d["parent_id"] in rect else y
            y = max(y, want)
            node(d, x, y)
            if d["parent_id"] in rect:
                edges.append((d["id"], d["parent_id"], "hosting", True))
            y += H + GAP_Y
        x += W + GAP_X

    states = {n["id"]: n["state"] for n in nodes}
    states.update({b["id"]: b["state"] for b in boxes})
    lines, seen = [], set()
    for a, b, typ, hard in edges:
        if a not in rect or b not in rect or a == b:
            continue
        if typ == "hosting" and kind(devs[a]) not in APPS:
            continue                                # maszyna w ramce serwera — bez linii
        if devs[a]["parent_id"] == b and kind(devs[b]) in HOSTS:
            continue                                # zależność od własnego serwera widać po ramce
        pair = (min(a, b), max(a, b), typ == "dane")
        if pair in seen:
            continue
        seen.add(pair)
        cls = "dane" if typ == "dane" else ("app" if typ == "hosting" else "")
        if typ == "dane" and states.get(b) == "down":
            cls += " broken"
        if not hard and typ != "dane":
            cls += " soft"
        lines.append({"d": _path(rect[a], rect[b]), "cls": cls.strip(), "dane": typ == "dane"})
    lines.sort(key=lambda e: e["dane"])             # dane na wierzchu
    height = max([n["y"] + H for n in nodes] + [b["y"] + b["h"] for b in boxes] + [TOP]) + 12
    hidden = sum(1 for d in devs.values() if kind(d) in CLIENTS)
    return {"nodes": nodes, "boxes": boxes, "lines": lines, "columns": columns, "width": max(x - GAP_X + LEFT + 60, 300),
            "height": height, "w": W, "h": H, "hidden_clients": hidden}


def _short(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def _path(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if ax + aw <= bx or bx + bw <= ax:              # obok siebie: z boku na bok
        if bx < ax:
            (ax, ay, aw, ah), (bx, by, bw, bh) = (bx, by, bw, bh), (ax, ay, aw, ah)
        y1, y2 = ay + min(ah, H) / 2, by + min(bh, H) / 2
        x1, x2 = ax + aw, bx
        mid = (x1 + x2) / 2
        return f"M {x1} {y1} C {mid} {y1}, {mid} {y2}, {x2} {y2}"
    # jedna nad drugą (np. dwie maszyny w tej samej ramce): łuk z prawej strony
    x = max(ax + aw, bx + bw)
    y1, y2 = ay + min(ah, H) / 2, by + min(bh, H) / 2
    bulge = x + 24 + min(abs(y2 - y1) / 8, 30)
    return f"M {ax + aw} {y1} C {bulge} {y1}, {bulge} {y2}, {bx + bw} {y2}"
