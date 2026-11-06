"""Mapa infrastruktury rysowana sama z dokumentacji — bez ręcznego układania (zasada „minimum ustawień”).

Układ jak w makiecie: od lewej internet → sieć (łańcuch: brama → router → switch → AP, każdy poziom zależności
to kolejna kolumna) → serwery fizyczne jako ramki z maszynami w środku (VM, LXC, NAS)
→ aplikacje (stacki, usługi) obok maszyny, na której działają. To, co w czymś działa, stoi w ramce — bez linii
do serwera. Linie:
- szara: sieć i kable (do ramki serwera, nie do każdej maszyny),
- niebieska przerywana: zależność „dane” (czerwona, gdy źródło danych nie działa),
- cienka szara: aplikacja → maszyna, na której działa.
Klienci sieci (komputery, telefony) domyślnie schowani; po włączeniu stoją pod routerem / AP.
Z lewej „z zewnątrz” (VPS) i internet od dostawcy (stan łącza z uplink.py, modem), od którego zależy brama.
Linie widać po najechaniu (przeglądarka), poza przerwanymi danymi. Kolor paska na kafelku = sieć adresu: VLAN z routera OpenWrt, a bez niego /24.
Układ można zmienić przeciąganiem: zapisane pozycje (kv map.layout) przesuwają ramkę z maszynami albo kafelek.

Każde urządzenie ma w data-rel listę tego, od czego zależy i co od niego zależy (przechodnio) — przeglądarka
podświetla po najechaniu cały łańcuch (static/denali.js). Tryb „co jeśli” (simulate=id) rysuje stan, jaki byłby,
gdyby to urządzenie padło: zamiast stanu na żywo — przyczyna, skutki twarde i to, co będzie działać gorzej.
"""
import ipaddress

from sqlalchemy import select

from . import infra_views, inventory as inv, uplink
from .db import kv_get, kv_set
from .models import cable, dependency, device, device_address

LAYOUT_KEY = "map.layout"
INTERNET_ID = "internet"
OUTSIDE = {"vps"}           # dostęp spoza domu — nie źródło internetu
INTERNET = {"modem"}
# kolory podsieci — rozróżnialne na ciemnym tle; kolejne sieci po kolei
NET_COLORS = ("#60a5fa", "#f472b6", "#a78bfa", "#fbbf24", "#34d399", "#fb923c", "#22d3ee", "#e879f9")
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


def simulation(conn, device_id):
    """Co by się stało, gdyby device_id padło: (znaczniki jak z incydentu, podsumowanie) albo (None, None)."""
    g = inv.load_graph(conn)
    if not inv.get_device(conn, device_id):
        return None, None
    hard, soft = g.impact(device_id)
    marks = {device_id: "przyczyna", **{d: "skutek" for d in hard}, **{d: "gorzej" for d in soft}}
    return marks, {"id": device_id, "name": g.name(device_id), "hard": sorted(g.name(d) for d in hard),
                   "soft": sorted(g.name(d) for d in soft)}


def build(conn, live, marks, with_clients=False, simulate=None):
    if simulate:
        live = {}                                   # w symulacji liczy się tylko to, co wynika z grafu
    devs = {d["id"]: dict(d) for d in conn.execute(
        select(device.c.id, device.c.name, device.c.kind, device.c.parent_id).order_by(device.c.name)).mappings()}
    ips = {}
    for r in conn.execute(select(device_address.c.device_id, device_address.c.value)
                          .where(device_address.c.kind == "ip").order_by(device_address.c.is_primary)):
        ips[r.device_id] = r.value
    edges = [(r.device_id, r.depends_on_id, r.type, bool(r.hard)) for r in conn.execute(select(dependency))]
    dep_edges = list(edges)                         # kable nie mają kierunku — poziomy sieci tylko z zależności
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
    layers = []                                     # to samo jako lista warstw — na wąski ekran telefonu

    routed = infra_views.router_networks(conn)

    def net_of(ip):
        """Etykieta sieci lokalnej adresu: „iot · VLAN 30 · 192.168.30.0/24” z routera albo samo /24. Publiczne — None."""
        net, name = infra_views.network_of(ip, routed) if ip else (None, None)
        if not net or not net.is_private:
            return None
        return f"{name} · {net}" if name else str(net)

    nets = {}                                       # sieć → kolor, w kolejności adresów
    for ip in sorted(ips.values(), key=_ip_key):
        n = net_of(ip)
        if n and n not in nets:
            nets[n] = NET_COLORS[len(nets) % len(NET_COLORS)]

    def node(d, x, y, sub=None, nested=False, box=None):
        rect[d["id"]] = (x, y, W, H)
        s = _state(d["id"], live, marks)
        net = net_of(ips.get(d["id"], ""))
        nodes.append({"id": d["id"], "x": x, "y": y, "name": d["name"], "short": _short(d["name"], 24),
                      "sub": sub or ips.get(d["id"]) or "", "kind": kind(d), "state": s, "label": STATE_LABEL.get(s, s),
                      "box": box, "net": net, "color": nets.get(net)})
        layers[-1]["items"].append({**nodes[-1], "nested": nested})

    def column(x, label):
        columns.append({"x": x, "label": label})
        if label:
            layers.append({"label": label, "items": []})

    columns, x = [], LEFT
    # 1: z zewnątrz — VPS to dostęp spoza domu
    col = [d for d in devs.values() if kind(d) in OUTSIDE]
    if col:
        column(x, "Z zewnątrz")
        for j, d in enumerate(col):
            node(d, x, TOP + j * (H + GAP_Y))
        x += W + GAP_X
    # 2: internet od dostawcy (stan łącza z uplink.py) i modem
    modems = [d for d in devs.values() if kind(d) in INTERNET]
    if modems or any(kind(d) in NETWORK for d in devs.values()):
        column(x, "Internet")
        s = "unknown" if simulate else ("down" if uplink.is_down(conn) else ("ok" if kv_get(conn, uplink.KEY) else "unknown"))
        rect[INTERNET_ID] = (x, TOP, W, H)
        nodes.append({"id": INTERNET_ID, "x": x, "y": TOP, "name": "Internet", "short": "Internet",
                      "sub": "łącze od dostawcy", "kind": "internet", "state": s, "label": STATE_LABEL.get(s, s),
                      "box": None, "net": None, "color": None})
        layers[-1]["items"].append({**nodes[-1], "nested": False})
        for j, d in enumerate(modems, 1):
            node(d, x, TOP + j * (H + GAP_Y))
            edges.append((d["id"], INTERNET_ID, "siec", True))
        x += W + GAP_X
    # 2: sieć jako łańcuch — urządzenie zależne od innego sieciowego (router za bramą, switch za routerem, AP za
    #    switchem) stoi kolumnę dalej, na wysokości tego, od którego zależy; klienci (gdy włączeni) pod ostatnią
    levels = _network_levels([d for d in devs.values() if kind(d) in NETWORK], dep_edges)
    clients = [d for d in devs.values() if kind(d) in CLIENTS] if with_clients else []
    if clients and not levels:
        levels = [[]]
    modem_ids = {d["id"] for d in modems}
    for depth, level in enumerate(levels):
        if depth == 0:              # brama wychodzi do internetu — przez modem, jeśli jest w dokumentacji
            for d, _up in level:
                if not any(e[0] == d["id"] and e[1] in modem_ids for e in dep_edges):
                    edges.append((d["id"], INTERNET_ID, "siec", True))
        column(x, "Sieć" if depth == 0 else "")
        y = TOP
        for d, up in sorted(level, key=lambda p: (rect[p[1]][1] if p[1] in rect else 0, p[0]["name"].lower())):
            y = max(y, rect[up][1] if up in rect else TOP)
            node(d, x, y)
            y += H + GAP_Y
        if depth == len(levels) - 1:
            for d in clients:
                node(d, x, y)
                y += H + GAP_Y
        x += W + GAP_X
    # 3: serwery jako ramki z maszynami; maszyny bez serwera pod ramkami
    boxes, y = [], TOP
    servers_x = x
    if hosts or loose:
        column(x, "Serwery i maszyny")
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
        layers[-1]["items"].append({"id": h["id"], "name": h["name"], "sub": ips.get(h["id"], ""), "kind": kind(h),
                                    "state": s, "label": STATE_LABEL.get(s, s), "nested": False})
        for i, d in enumerate(kids):
            c, r = divmod(i, per_col)
            node(d, x + BOX_PAD + c * (W + BOX_PAD), y + BOX_HEAD + BOX_PAD + r * (H + GAP_Y), nested=True, box=h["id"])
        y += bh + BOX_GAP
    for d in loose:
        node(d, x, y)
        y += H + GAP_Y
    servers_w = max([b["w"] for b in boxes] + ([W] if hosts or loose else [0]))
    x = servers_x + servers_w + (GAP_X if servers_w else 0)
    # 4: aplikacje na wysokości maszyny, na której działają
    if apps:
        column(x, "Aplikacje")
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

    # 5: układ ułożony ręcznie — ramka przesuwa się razem z maszynami w środku
    saved = kv_get(conn, LAYOUT_KEY) or {}
    for b in boxes:
        if str(b["id"]) in saved:
            dx, dy = saved[str(b["id"])][0] - b["x"], saved[str(b["id"])][1] - b["y"]
            b["x"], b["y"] = b["x"] + dx, b["y"] + dy
            rect[b["id"]] = (b["x"], b["y"], b["w"], b["h"])
            for n in nodes:
                if n["box"] == b["id"]:
                    n["x"], n["y"] = n["x"] + dx, n["y"] + dy
                    rect[n["id"]] = (n["x"], n["y"], W, H)
    for n in nodes:
        if n["box"] is None and str(n["id"]) in saved:
            n["x"], n["y"] = saved[str(n["id"])]
            rect[n["id"]] = (n["x"], n["y"], W, H)

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
        pair = (min(str(a), str(b)), max(str(a), str(b)), typ == "dane")
        if pair in seen:
            continue
        seen.add(pair)
        cls = "dane" if typ == "dane" else ("app" if typ == "hosting" else "")
        if typ == "dane" and states.get(b) == "down":
            cls += " broken"
        if not hard and typ != "dane":
            cls += " soft"
        lines.append({"d": _path(rect[a], rect[b]), "cls": cls.strip(), "dane": typ == "dane", "a": a, "b": b})
    lines.sort(key=lambda e: e["dane"])             # dane na wierzchu
    g = inv.load_graph(conn)
    for n in nodes + boxes:
        if n["id"] == INTERNET_ID:      # internet: cała sieć, która przez niego wychodzi
            n["rel"] = " ".join(str(d["id"]) for d in devs.values() if kind(d) in NETWORK | INTERNET)
            continue
        hard, soft = g.impact(n["id"])
        n["rel"] = " ".join(sorted(str(x) for x in g.ancestors(n["id"], hard_only=False) | hard | soft
                                   | {e[0] for e in edges if e[1] == n["id"]}
                                   | {e[1] for e in edges if e[0] == n["id"]}))
    height = max([n["y"] + H for n in nodes] + [b["y"] + b["h"] for b in boxes] + [TOP]) + 12
    width = max([x - GAP_X + LEFT + 60, 300] + [n["x"] + W + 60 for n in nodes] + [b["x"] + b["w"] + 60 for b in boxes])
    hidden = sum(1 for d in devs.values() if kind(d) in CLIENTS)
    return {"nodes": nodes, "boxes": boxes, "lines": lines, "columns": columns, "layers": layers, "width": width,
            "height": height, "w": W, "h": H, "hidden_clients": hidden, "nets": nets, "arranged": bool(saved)}


def save_layout(conn, positions):
    """positions: {id: [x, y]} lewego górnego rogu ramki albo kafelka (dopisywane do zapisanych); None = automatyczny."""
    if positions is None:
        kv_set(conn, LAYOUT_KEY, {})
        return
    clean = {}
    for k, v in positions.items():
        if (str(k).isdigit() or k == INTERNET_ID) and isinstance(v, (list, tuple)) and len(v) == 2 \
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 20000 for c in v):
            clean[str(k)] = [round(v[0]), round(v[1])]
    kv_set(conn, LAYOUT_KEY, {**(kv_get(conn, LAYOUT_KEY) or {}), **clean})


def _ip_key(ip):
    try:
        return (0, int(ipaddress.ip_address(ip)))
    except ValueError:
        return (1, 0)


def _network_levels(net, edges):
    """[[(urządzenie, id tego, od którego zależy, albo None)], …] — poziom = najdłuższy łańcuch zależności sieciowych."""
    ids = {d["id"] for d in net}
    ups = {}
    for a, b, _typ, _hard in edges:
        if a in ids and b in ids and a != b:
            ups.setdefault(a, set()).add(b)
    depth = {}

    def level(i, path=frozenset()):
        if i not in depth:
            depth[i] = max((level(u, path | {i}) + 1 for u in ups.get(i, ()) if u not in path), default=0)
        return depth[i]

    out = []
    for d in net:
        k = level(d["id"])
        up = max((u for u in ups.get(d["id"], ()) if depth.get(u) == k - 1), default=None)
        while len(out) <= k:
            out.append([])
        out[k].append((d, up))
    return out


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
