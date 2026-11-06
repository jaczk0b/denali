"""Zakładka Źródła: każda integracja w tym samym układzie — zamiast osobnej zakładki z innym wyglądem dla każdej.

Wspólny kształt jednego źródła:
    {"row": integracja, "label", "state", "open_url", "device_id",
     "kpis":   [{"label", "value", "sub", "pct", "state"}],          kafle podsumowania
     "checks": [{"state", "text"}],                                 „do sprawdzenia” — z alarmów, które liczy adapter
     "tables": [{"title", "rows": [row]}]}
    row = {"state", "name", "sub", "device_id", "ip", "net", "meters": [{"label", "pct", "text"}],
           "details": [tekst], "eid"}
Tabele mają te same kolumny dla każdego źródła: Stan · Nazwa · Adres i sieć · Zasoby · Szczegóły · Akcje.
Integracja bez własnego opisu poniżej dostaje widok ogólny ze swoich stanów — nowa integracja pojawia się sama.
"""
import time

from sqlalchemy import select

from . import infra_views as views
from .adapters import registry
from .models import device_address, integration, observed_state, update_item

ORDER = ("proxmox", "pbs", "truenas", "komodo", "openwrt", "tplink", "ovh", "agent")
NET_COLORS = ("#60a5fa", "#f472b6", "#a78bfa", "#fbbf24", "#34d399", "#fb923c", "#22d3ee", "#e879f9")


def _when(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def _gb(b):
    return f"{b / 1024 ** 3:.1f} GB".replace(".", ",") if isinstance(b, (int, float)) else "—"


def _meter(label, pct, text=""):
    return {"label": label, "pct": max(0.0, min(1.0, pct)), "text": text or f"{pct * 100:.0f}%"} \
        if isinstance(pct, (int, float)) else None


def _alarm_checks(states):
    out = []
    for s in states.values():
        for a in (s["data"] or {}).get("alarms") or []:
            if isinstance(a, dict) and a.get("text"):
                out.append({"state": "down" if a.get("severity") == "krytyczny" else "warn", "text": str(a["text"])})
    return out


class Ctx:
    """To, czego potrzebuje każdy opis źródła: adresy urządzeń i sieci (VLAN-y) z routera."""

    def __init__(self, conn, vendor=None):
        self.conn = conn
        self.vendor = vendor or (lambda mac: None)
        self.ips = {}
        for r in conn.execute(select(device_address.c.device_id, device_address.c.value)
                              .where(device_address.c.kind == "ip").order_by(device_address.c.is_primary)):
            self.ips[r.device_id] = r.value
        self.routed = views.router_networks(conn)
        self.colors = {}
        for i, n in enumerate(sorted(self.routed, key=lambda n: int(n["net"].network_address))):
            self.colors[n["net"]] = NET_COLORS[i % len(NET_COLORS)]

    def addr(self, device_id=None, ip=None):
        ip = ip or self.ips.get(device_id) or ""
        if not ip:
            return {"ip": "", "net": None}
        net, name = views.network_of(ip, self.routed)
        if net is None or not net.is_private:
            return {"ip": ip, "net": None}
        return {"ip": ip, "net": {"label": name or str(net), "cidr": str(net), "color": self.colors.get(net)}}


def _row(ctx, state, name, *, sub="", device_id=None, ip=None, meters=(), details=(), eid="", links=()):
    """links: [(tekst, adres)] — przyciski w kolumnie Akcje (np. terminal SSH), tylko dla administratora."""
    return {"state": state or "unknown", "name": name, "sub": sub, "device_id": device_id, "eid": eid,
            **ctx.addr(device_id, ip), "meters": [m for m in meters if m], "details": [d for d in details if d],
            "links": list(links)}


# ───────────────────────── opisy źródeł ─────────────────────────
def _proxmox(ctx, row):
    c = next((x for x in views.proxmox(ctx.conn) if x["row"]["id"] == row["id"]), None)
    if not c:
        return {}
    nodes = [_row(ctx, n["state"], n["name"], sub="węzeł", device_id=n["device_id"], eid=n["eid"],
                  meters=[_meter("CPU", n["cpu"]), _meter("RAM", n["ram"], f"{_gb(n['mem'])} / {_gb(n['maxmem'])}")],
                  details=[f"działa {n['uptime'] // 86400} dni" if n["uptime"] else n["summary"],
                           f"maszyny: {n['running']} z {n['guests']} działa"]) for n in c["nodes"]]
    guests = []
    for g in c["guests"]:
        b = g["backup"]
        backup = None
        if b:
            last = (b["data"] or {}).get("last_backup")
            backup = f"kopia PBS: {_when(last)}" + (" — za stara" if b["state"] != "ok" else "")
        guests.append(_row(ctx, g["state"], g["name"], device_id=g["device_id"], eid=g["eid"],
                           sub=f"{'VM' if g['type'] == 'qemu' else 'LXC'} {g['vmid']} · {g['node'] or '?'}",
                           meters=[_meter("CPU", g["cpu"]), _meter("RAM", g["ram"], f"{_gb(g['mem'])} / {_gb(g['maxmem'])}"),
                                   _meter("Dysk", views._pct(g["disk"], g["maxdisk"]))],
                           details=[g["summary"], backup or "brak kopii w PBS",
                                    "startuje z hostem" if g["onboot"] else None]))
    running = sum(1 for g in c["guests"] if g["status"] == "running")
    kpis = [{"label": "Maszyny i kontenery", "value": f"{running} / {len(c['guests'])}", "sub": "działa"}]
    for n in c["nodes"]:
        kpis.append({"label": f"Węzeł {n['name']}", "value": f"{(n['cpu'] or 0) * 100:.0f}% CPU" if n["cpu"] is not None else "—",
                     "sub": f"RAM {_gb(n['mem'])} / {_gb(n['maxmem'])}", "pct": n["ram"], "state": n["state"]})
    checks = []
    if c["limited"]:
        checks.append({"state": "warn", "text": "Token Proxmoxa ma za mało uprawnień: brak CPU, RAM albo maszyn. "
                                                "Nadaj rolę PVEAuditor na „/” (instrukcja na stronie integracji)."})
    return {"open_url": (row["config"] or {}).get("url"), "kpis": kpis, "checks": checks,
            "tables": [{"title": "Węzły", "rows": nodes}, {"title": "Maszyny i kontenery", "rows": guests}]}


def _pbs(ctx, row):
    src = next((s for s in views.sources(ctx.conn, "pbs") if s["row"]["id"] == row["id"]), None)
    if not src:
        return {}
    server = src["states"].get("server")
    usage = ((server["data"] or {}).get("usage") or {}) if server else {}
    kpis = [{"label": f"Magazyn {name}", "value": f"{u['used'] / u['total'] * 100:.0f}%",
             "sub": f"{_gb(u['used'])} / {_gb(u['total'])}", "pct": u["used"] / u["total"],
             "state": "warn" if u["used"] / u["total"] >= .85 else "ok"} for name, u in sorted(usage.items()) if u.get("total")]
    guests = {g["eid"]: g for c in views.proxmox(ctx.conn) for g in c["guests"]}
    rows = []
    for eid, s in sorted(src["states"].items()):
        if not eid.startswith("backup/"):
            continue
        _b, typ, vmid = (eid.split("/") + ["", ""])[:3]
        g = guests.get(f"{'qemu' if typ == 'vm' else 'lxc'}/{vmid}")
        d = s["data"] or {}
        rows.append(_row(ctx, s["state"], g["name"] if g else f"{typ.upper()} {vmid}", device_id=g["device_id"] if g else None,
                         sub=f"{'VM' if typ == 'vm' else 'LXC'} {vmid} · magazyn {d.get('store') or '?'}", eid=eid,
                         details=[f"ostatnia kopia: {_when(d.get('last_backup'))}", f"kopii: {d.get('count', '?')}"]))
    kpis.insert(0, {"label": "Maszyny z kopią", "value": str(len(rows)),
                    "sub": f"świeże: {sum(1 for r in rows if r['state'] == 'ok')}"})
    return {"kpis": kpis, "checks": _alarm_checks(src["states"]), "tables": [{"title": "Kopie maszyn", "rows": rows}],
            "device_id": views._device_of(src, "server")}


def _truenas(ctx, row):
    t = next((x for x in views.truenas(ctx.conn) if x["row"]["id"] == row["id"]), None)
    if not t:
        return {}
    pools = [_row(ctx, p["state"], p["name"], sub=p["status"] or "pula",
                  meters=[_meter("Zajęte", p["used"])], details=[p["summary"]]) for p in t["pools"]]
    disks = [_row(ctx, d["state"], d.get("name") or "?",
                  sub=" · ".join(str(x) for x in (d.get("type"), d.get("model"), d.get("serial")) if x),
                  details=[f"pula {d['pool']}" if d.get("pool") else "poza pulą",
                           f"{d['temp']} °C" if d.get("temp") is not None else None,
                           "SMART: " + " · ".join(str(x) for x in ((d.get("smart") or {}).get("desc"), (d.get("smart") or {}).get("status")) if x)
                           if d.get("smart") else "brak testów SMART"]) for d in t["disks"]]
    tasks = [_row(ctx, x["state"], x["key"], sub=x["kind"], details=[x["summary"]]) for x in t["tasks"]]
    checks = [{"state": {"INFO": "unknown", "WARNING": "warn", "NOTICE": "warn"}.get(a.get("level"), "down"),
               "text": f"TrueNAS ({views.TN_LEVELS.get(a.get('level'), a.get('level'))}): {a.get('text')}"}
              for a in t["data"].get("alert_list") or []]
    src = next(s for s in views.sources(ctx.conn, "truenas") if s["row"]["id"] == row["id"])
    kpis = [{"label": f"Pula {p['name']}", "value": f"{(p['used'] or 0) * 100:.0f}%", "sub": p["status"] or "",
             "pct": p["used"], "state": p["state"]} for p in t["pools"]]
    kpis.append({"label": "Dyski", "value": str(len(t["disks"])),
                 "sub": f"z problemem: {sum(1 for d in t['disks'] if d['state'] != 'ok')}"})
    if t["consumers"]:
        kpis.append({"label": "Korzysta z danych", "value": str(len(t["consumers"])),
                     "sub": ", ".join(c["name"] for c in t["consumers"][:3])})
    return {"device_id": t["device_id"], "kpis": kpis, "checks": _alarm_checks(src["states"]) + checks,
            "tables": [{"title": "Pule", "rows": pools}, {"title": "Dyski", "rows": disks},
                       {"title": "Snapshoty i replikacja", "rows": tasks}]}


def _komodo(ctx, row):
    k = next((x for x in views.docker(ctx.conn) if x["row"]["id"] == row["id"]), None)
    if not k:
        return {}
    servers, stacks = [], []
    for g in k["groups"]:
        if g["eid"]:
            servers.append(_row(ctx, g["state"], g["name"], sub="serwer Komodo", device_id=g["device_id"], eid=g["eid"],
                                ip=(g["address"] or "").split("//")[-1].split(":")[0] or None,
                                details=[g["summary"], f"stacków: {len(g['stacks'])}"]))
        for s in g["stacks"]:
            imgs = [f"{i['name']}:{i['tag']}" + (" — nowszy obraz" if i.get("service") in s["updates"] else "")
                    for i in s["images"]]
            stacks.append(_row(ctx, s["state"], s["name"], sub=f"stack · {g['name']}", device_id=s["device_id"], eid=s["eid"],
                               ip=(g["address"] or "").split("//")[-1].split(":")[0] or None,
                               details=[s["summary"]] + imgs))
    n = k["count"]
    kpis = [{"label": "Stacki", "value": f"{n['stacks'] - n['problem']} / {n['stacks']}", "sub": "bez problemu"},
            {"label": "Serwery", "value": str(n["servers"]), "sub": ""},
            {"label": "Nowsze obrazy", "value": str(n["updates"]), "sub": "stacki z aktualizacją",
             "state": "warn" if n["updates"] else "ok"}]
    src = next(s for s in views.sources(ctx.conn, "komodo") if s["row"]["id"] == row["id"])
    return {"open_url": (row["config"] or {}).get("url"), "kpis": kpis, "checks": _alarm_checks(src["states"]),
            "tables": [{"title": "Stacki", "rows": stacks}, {"title": "Serwery", "rows": servers}]}


def _network(ctx, row):
    n = next((x for x in views.network(ctx.conn) if x["row"]["id"] == row["id"]), None)
    if not n:
        return {}
    d, cnt = n["data"], n["count"]
    wan = d.get("wan") or {}
    kpis = [{"label": "Router", "value": d.get("model") or "—", "sub": d.get("firmware") or ""},
            {"label": "WAN", "value": wan.get("ip") or ("działa" if wan.get("up") else "—"),
             "sub": wan.get("proto") or "", "state": "ok" if wan.get("up") else ("down" if wan else None)},
            {"label": "Klienci", "value": str(cnt["all"]), "sub": f"Wi-Fi {cnt['wifi']} · kabel {cnt['wired']}"}]
    nets = [_row(ctx, "ok", x["name"], sub=f"VLAN {x['vlan']}" if x.get("vlan") else (x.get("device") or "sieć"),
                 ip=x.get("gateway"), details=[x["cidr"], f"brama {x.get('gateway')}"]) for x in d.get("networks") or []]
    ports = [_row(ctx, "ok" if p["up"] else "off", p["name"], sub="port",
                  details=[p["speed"] or ("połączony" if p["up"] else "brak kabla")]) for p in d.get("ports") or []]
    clients = [_row(ctx, c["state"], c["device"] or c["name"], device_id=c["device_id"], ip=c["ip"],
                    sub=" · ".join(x for x in (c["mac"], ctx.vendor(c["mac"])) if x), details=[c["connection"] + (f" „{c['ssid']}”" if c["ssid"] else ""),
                                           f"sygnał {c['signal']} dBm" + (" — słaby" if isinstance(c["signal"], (int, float)) and c["signal"] < views.WEAK_SIGNAL else "")
                                           if c["signal"] is not None else None,
                                           "w kolejce do zatwierdzenia" if c["status"] == "nowy" else None])
               for c in n["clients"]]
    checks = [{"state": "warn", "text": f"Słaby sygnał Wi-Fi (poniżej {views.WEAK_SIGNAL} dBm): {cnt['weak']} urządz."}] if cnt["weak"] else []
    if d.get("updates"):
        checks.append({"state": "warn", "text": f"Pakiety do aktualizacji na routerze: {d['updates']}"})
    return {"open_url": (row["config"] or {}).get("url"), "device_id": n["device_id"], "kpis": kpis, "checks": checks,
            "tables": [{"title": "Sieci (VLAN-y)", "rows": nets}, {"title": "Porty", "rows": ports},
                       {"title": "Kto jest w sieci", "rows": clients}]}


def _ovh(ctx, row):
    o = next((x for x in views.vps(ctx.conn) if x["row"]["id"] == row["id"]), None)
    if not o:
        return {}
    rows = []
    for v in o["items"]:
        d = v["data"]
        ips = d.get("ips") or []
        res = " · ".join(x for x in (f"{d['vcore']} vCPU" if d.get("vcore") else "",
                                     f"{round(d['memory_mb'] / 1024, 1)} GB RAM" if d.get("memory_mb") else "",
                                     f"{d['disk_gb']} GB dysk" if d.get("disk_gb") else "") if x)
        links = [("Terminal SSH", f"/terminale/{v['device_id']}")] if v["device_id"] else             [("Zatwierdź, by mieć terminal", "/zatwierdz")] if v["waiting"] else []
        rows.append(_row(ctx, v["state"], v["name"], device_id=v["device_id"], sub=d.get("offer") or d.get("plan") or "VPS",
                         ip=ips[0] if ips else None, links=links,
                         details=[d.get("state_text"), res, d.get("location"), f"opłata: {d['payment']}" if d.get("payment") else None,
                                  f"umowa do {d['engaged_to']}" if d.get("engaged_to") else None,
                                  "kopia automatyczna" + (f" ({d['backup_at']})" if d.get("backup_at") else "") if d.get("backup") else "brak automatycznej kopii",
                                  f"snapshot: {d.get('snapshot_at') or 'brak'}",
                                  f"blokada: {d['locked']}" if d.get("locked") else None,
                                  " · ".join(ips[1:]) if len(ips) > 1 else None]))
    acc = (o["account"]["data"] or {}) if o["account"] else {}
    kpis = [{"label": "VPS", "value": str(len(rows)), "sub": f"działa: {sum(1 for r in rows if r['state'] == 'ok')}"},
            {"label": "Zaległości", "value": acc.get("due") or "—", "sub": "na koncie OVH",
             "state": "warn" if acc.get("due_value") else "ok"}]
    bills = [_row(ctx, "ok", b.get("id"), sub=b.get("date"), details=[b.get("amount")]) for b in acc.get("bills") or []]
    return {"open_url": o["manager"], "kpis": kpis, "checks": [],
            "tables": [{"title": "Serwery", "rows": rows}, {"title": "Rachunki", "rows": bills}]}


def _agents(ctx, rows):
    """Wszyscy agenci jako jedno źródło: maszyny, aktualizacje, restart, dyski."""
    out = []
    pending = {}
    for r in ctx.conn.execute(select(update_item.c.integration_id).where(update_item.c.integration_id.in_([x["id"] for x in rows]))):
        pending[r.integration_id] = pending.get(r.integration_id, 0) + 1
    checks = []
    for src in views.sources(ctx.conn, "agent"):
        s = src["states"].get("host")
        d = (s["data"] or {}) if s else {}
        disks = d.get("disks") or []
        worst_disk = max(disks, key=lambda x: x.get("pct") or 0) if disks else None
        mem = (d.get("memory") or {}).get("memory")
        did = views._device_of(src, "host")
        out.append(_row(ctx, s["state"] if s else "unknown", src["row"]["name"], device_id=did,
                        ip=(d.get("ips") or [None])[0], sub=d.get("os") or "agent",
                        meters=[_meter("RAM", mem / 100 if isinstance(mem, (int, float)) else None),
                                _meter(f"Dysk {worst_disk['mount']}", (worst_disk.get('pct') or 0) / 100) if worst_disk else None],
                        details=[f"aktualizacje: {pending.get(src['row']['id'], 0)}",
                                 "wymagany restart" if d.get("reboot_required") else None,
                                 f"usługi failed: {len(d['failed_units'])}" if d.get("failed_units") else None,
                                 f"raport {_when(d.get('reported_at'))}" if s else "jeszcze bez raportu"]))
        checks += _alarm_checks(src["states"])
    kpis = [{"label": "Maszyny z agentem", "value": str(len(out)), "sub": f"raportuje: {sum(1 for r in out if r['state'] != 'unknown')}"},
            {"label": "Do aktualizacji", "value": str(sum(1 for v in pending.values() if v)), "sub": "maszyn z pakietami"},
            {"label": "Wymagany restart", "value": str(sum(1 for r in out if "wymagany restart" in r["details"])), "sub": ""}]
    return {"kpis": kpis, "checks": checks, "tables": [{"title": "Maszyny", "rows": out}]}


def _generic(ctx, row):
    src = next(s for s in views.sources(ctx.conn, row["type"]) if s["row"]["id"] == row["id"])
    rows = [_row(ctx, s["state"], views._name(src, eid) or eid, sub=eid, device_id=views._device_of(src, eid), eid=eid,
                 details=[s["summary"]]) for eid, s in sorted(src["states"].items())]
    return {"kpis": [{"label": "Obiekty", "value": str(len(rows)), "sub": f"działa: {sum(1 for r in rows if r['state'] == 'ok')}"}],
            "checks": _alarm_checks(src["states"]), "tables": [{"title": "Obiekty", "rows": rows}]}


BUILDERS = {"proxmox": _proxmox, "pbs": _pbs, "truenas": _truenas, "komodo": _komodo, "openwrt": _network,
            "tplink": _network, "ovh": _ovh}


def overview(conn):
    """Kafelki wszystkich źródeł do przełącznika: [{"key", "name", "label", "type", "state", "problems", "error"}].
    Agenci są jednym kafelkiem (key "agenci")."""
    rows = [dict(r) for r in conn.execute(select(integration)).mappings()]
    types = registry()
    out, agents = [], [r for r in rows if r["type"] == "agent"]
    for r in sorted((r for r in rows if r["type"] != "agent"),
                    key=lambda r: (ORDER.index(r["type"]) if r["type"] in ORDER else 99, r["name"].lower())):
        states = [s.state for s in conn.execute(select(observed_state.c.state)
                                                .where(observed_state.c.integration_id == r["id"]))]
        out.append({"key": str(r["id"]), "name": r["name"], "type": r["type"],
                    "label": types[r["type"]].label if r["type"] in types else r["type"],
                    "state": "down" if r["last_error"] else views.worst(states),
                    "problems": sum(1 for s in states if s in ("down", "warn")), "error": r["last_error"],
                    "enabled": r["enabled"]})
    if agents:
        states = [s.state for s in conn.execute(select(observed_state.c.state).where(
            observed_state.c.integration_id.in_([a["id"] for a in agents])))]
        out.append({"key": "agenci", "name": "Agenci", "type": "agent", "label": f"{len(agents)} maszyn",
                    "state": views.worst(states), "problems": sum(1 for s in states if s in ("down", "warn")),
                    "error": None, "enabled": True})
    return out


def build(conn, key, vendor=None):
    """Pełny widok jednego źródła (key z overview) albo None. vendor(mac) → producent karty sieciowej (oui)."""
    ctx = Ctx(conn, vendor)
    if key == "agenci":
        rows = [dict(r) for r in conn.execute(select(integration).where(integration.c.type == "agent")).mappings()]
        if not rows:
            return None
        return {"row": None, "name": "Agenci na maszynach", "label": "agent", "open_url": None, "device_id": None,
                **_agents(ctx, rows)}
    row = conn.execute(select(integration).where(integration.c.id == int(key))).mappings().first() \
        if str(key).isdigit() else None
    if not row:
        return None
    types = registry()
    view = (BUILDERS.get(row["type"]) or _generic)(ctx, row) or _generic(ctx, row)
    return {"row": row, "name": row["name"], "label": types[row["type"]].label if row["type"] in types else row["type"],
            "open_url": None, "device_id": None, "kpis": [], "checks": [], "tables": [], **view}
