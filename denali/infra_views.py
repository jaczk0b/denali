"""Zakładki działu Infrastruktura: Proxmox, TrueNAS, Docker, Sieć, Adresy IP.

Tylko odczyt — z tego, co integracje już zapisały (observed_state) i z dokumentacji. Nic tu nie pyta serwerów:
strona pokazuje stan z ostatniego odczytu workera, a przy błędzie źródła mówi, że dane mogą być nieaktualne.
"""
import ipaddress

from sqlalchemy import select

from .adapters.tplink import BAND
from .models import dependency, device, device_address, external_ref, integration, observed_state, update_item

TABS = [("mapa", "Mapa", "/infrastruktura/mapa"), ("lista", "Lista", "/urzadzenia"),
        ("proxmox", "Proxmox", "/infrastruktura/proxmox"), ("truenas", "TrueNAS", "/infrastruktura/truenas"),
        ("docker", "Docker", "/infrastruktura/docker"), ("siec", "Sieć", "/infrastruktura/siec"),
        ("adresy", "Adresy IP", "/infrastruktura/adresy"), ("szafa", "Szafa", "/infrastruktura/szafa")]
# Zakładka źródła jest widoczna dopiero, gdy jest podłączona integracja tego typu — bez pustych zakładek.
TAB_SOURCES = {"proxmox": ("proxmox",), "truenas": ("truenas",), "docker": ("komodo",), "siec": ("tplink", "openwrt")}
WORST = ("down", "warn", "unknown", "off", "ok")


def tabs(conn):
    present = {r.type for r in conn.execute(select(integration.c.type).distinct())}
    return [(key, label, url) for key, label, url in TABS
            if key not in TAB_SOURCES or present & set(TAB_SOURCES[key])]


def worst(states):
    states = [s for s in states if s]
    return next((w for w in WORST if w in states), "unknown")


def sources(conn, *types):
    """[{"row": integracja, "states": {external_id: stan}, "refs": {external_id: external_ref}}] — po nazwie."""
    out = []
    for row in conn.execute(select(integration).where(integration.c.type.in_(types))
                            .order_by(integration.c.name)).mappings():
        states = {r["external_id"]: r for r in conn.execute(
            select(observed_state).where(observed_state.c.integration_id == row["id"])).mappings()}
        refs = {r["external_id"]: r for r in conn.execute(
            select(external_ref).where(external_ref.c.integration_id == row["id"])).mappings()}
        out.append({"row": row, "states": states, "refs": refs})
    return out


def _device_of(src, eid):
    ref = src["refs"].get(eid)
    return ref["device_id"] if ref and ref["status"] == "zatwierdzony" else None


def _name(src, eid):
    ref = src["refs"].get(eid)
    return (ref["proposed"] or {}).get("name") if ref else None


def _pct(used, total):
    return used / total if used is not None and total else None


# ───────────────────────── Proxmox ─────────────────────────
def proxmox(conn):
    backups = {}    # (typ, vmid) → dane ostatniej kopii z PBS
    for src in sources(conn, "pbs"):
        for eid, s in src["states"].items():
            parts = eid.split("/")
            if len(parts) == 3 and parts[0] == "backup":
                key = ("qemu" if parts[1] == "vm" else "lxc", parts[2])
                prev = backups.get(key)
                if not prev or ((s["data"] or {}).get("last_backup") or 0) > ((prev["data"] or {}).get("last_backup") or 0):
                    backups[key] = s
    out = []
    for src in sources(conn, "proxmox"):
        nodes, guests = [], []
        for eid, s in sorted(src["states"].items()):
            d = s["data"] or {}
            if eid.startswith("node/"):
                nodes.append({"eid": eid, "name": eid.split("/", 1)[1], "state": s["state"], "summary": s["summary"],
                              "device_id": _device_of(src, eid), "cpu": d.get("cpu"),
                              "mem": d.get("mem"), "maxmem": d.get("maxmem"), "ram": _pct(d.get("mem"), d.get("maxmem")),
                              "uptime": d.get("uptime"), "guests": 0, "running": 0})
            elif eid.startswith(("qemu/", "lxc/")):
                typ, vmid = eid.split("/", 1)
                guests.append({"eid": eid, "type": typ, "vmid": int(vmid) if vmid.isdigit() else vmid,
                               "name": _name(src, eid) or eid, "device_id": _device_of(src, eid),
                               "state": s["state"], "summary": s["summary"], "status": d.get("status"),
                               "node": d.get("node"), "cpu": d.get("cpu"), "mem": d.get("mem"),
                               "maxmem": d.get("maxmem"), "ram": _pct(d.get("mem"), d.get("maxmem")),
                               "disk": d.get("disk"), "maxdisk": d.get("maxdisk"), "uptime": d.get("uptime"),
                               "onboot": d.get("onboot"), "backup": backups.get((typ, vmid))})
        by_node = {n["name"]: n for n in nodes}
        for g in guests:
            n = by_node.get(g["node"])
            if n:
                n["guests"] += 1
                n["running"] += g["status"] == "running"
        guests.sort(key=lambda g: (WORST.index(g["state"]) if g["state"] in WORST else 9, str(g["vmid"])))
        out.append({"row": src["row"], "nodes": nodes, "guests": guests})
    return out


# ───────────────────────── TrueNAS ─────────────────────────
def _consumers(conn, device_id):
    """Kto korzysta z danych tego urządzenia: zależności typu „dane” (ręczne i wykryte przez agenta)."""
    if not device_id:
        return []
    return conn.execute(select(device.c.id, device.c.name, dependency.c.note, dependency.c.source)
                        .join(dependency, dependency.c.device_id == device.c.id)
                        .where(dependency.c.depends_on_id == device_id, dependency.c.type == "dane")
                        .order_by(device.c.name)).mappings().all()


def truenas(conn):
    out = []
    for src in sources(conn, "truenas"):
        server = src["states"].get("server")
        pools, tasks = [], []
        for eid, s in sorted(src["states"].items()):
            d = s["data"] or {}
            if eid.startswith("pool/"):
                pools.append({"name": eid.split("/", 1)[1], "state": s["state"], "summary": s["summary"],
                              "status": d.get("status"), "used": d.get("used"), "size": d.get("size"),
                              "allocated": d.get("allocated")})
            elif eid.startswith(("snapshot/", "replication/")):
                kind, key = eid.split("/", 1)
                tasks.append({"kind": "snapshoty" if kind == "snapshot" else "replikacja", "key": key,
                              "state": s["state"], "summary": s["summary"], "last": d.get("last")})
        device_id = _device_of(src, "server")
        out.append({"row": src["row"], "server": server, "data": (server["data"] or {}) if server else {},
                    "device_id": device_id, "pools": pools, "tasks": tasks,
                    "consumers": _consumers(conn, device_id)})
    return out


# ───────────────────────── Docker (Komodo) ─────────────────────────
def docker(conn):
    out = []
    for src in sources(conn, "komodo"):
        images = {}
        for r in conn.execute(select(update_item).where(update_item.c.integration_id == src["row"]["id"])).mappings():
            images.setdefault(r["external_id"], []).append(r["component"])
        servers = {}
        for eid, s in src["states"].items():
            if eid.startswith("server/"):
                servers[eid] = {"eid": eid, "name": _name(src, eid) or eid, "device_id": _device_of(src, eid),
                                "state": s["state"], "summary": s["summary"],
                                "address": (s["data"] or {}).get("address"), "stacks": []}
        loose = {"eid": "", "name": "bez serwera", "device_id": None, "state": "unknown", "summary": "",
                 "address": None, "stacks": []}
        for eid, s in src["states"].items():
            if not eid.startswith("stack/"):
                continue
            ref = src["refs"].get(eid)
            parent = ((ref["proposed"] or {}).get("parent") or {}).get("ref") if ref else None
            d = s["data"] or {}
            (servers.get(parent) or loose)["stacks"].append({
                "eid": eid, "name": _name(src, eid) or eid, "device_id": _device_of(src, eid), "state": s["state"],
                "summary": s["summary"], "komodo_state": d.get("komodo_state"), "services": d.get("services"),
                "updates": images.get(eid, [])})
        groups = sorted(servers.values(), key=lambda g: g["name"].lower()) + ([loose] if loose["stacks"] else [])
        for g in groups:
            g["stacks"].sort(key=lambda x: (WORST.index(x["state"]) if x["state"] in WORST else 9, x["name"].lower()))
        stacks = [x for g in groups for x in g["stacks"]]
        out.append({"row": src["row"], "groups": groups, "count": {
            "servers": len(servers), "stacks": len(stacks),
            "problem": sum(1 for x in stacks if x["state"] in ("down", "warn")),
            "updates": sum(1 for x in stacks if x["updates"])}})
    return out


# ───────────────────────── Sieć ─────────────────────────
def network(conn):
    names = {r.id: r.name for r in conn.execute(select(device.c.id, device.c.name))}
    out = []
    for src in sources(conn, "tplink", "openwrt"):
        router = src["states"].get("router")
        clients = []
        for eid, s in src["states"].items():
            if not eid.startswith("mac/"):
                continue
            ref = src["refs"].get(eid)
            prop = (ref["proposed"] if ref else None) or {}
            d = s["data"] or {}
            ips = [a["value"] for a in prop.get("addresses") or [] if a.get("kind") == "ip"]
            did = _device_of(src, eid)
            clients.append({"mac": eid.split("/", 1)[1], "ip": ips[0] if ips else "", "name": prop.get("name") or eid,
                            "device_id": did, "device": names.get(did), "status": ref["status"] if ref else "nowy",
                            "ref_id": ref["id"] if ref else None, "state": s["state"],
                            "connection": BAND.get(d.get("connection") or "", d.get("connection") or "—"),
                            "signal": d.get("signal"),
                            "wired": (d.get("connection") or "") == "WIRED"})
        clients.sort(key=lambda c: (c["status"] != "nowy", _ip_key(c["ip"]), c["name"].lower()))
        rdata = (router["data"] or {}) if router else {}
        out.append({"row": src["row"], "router": router, "data": rdata, "device_id": _device_of(src, "router"),
                    "as_ap": bool((src["row"]["config"] or {}).get("as_ap")), "clients": clients,
                    "count": {"all": len(clients), "new": sum(1 for c in clients if c["status"] == "nowy"),
                              "wifi": sum(1 for c in clients if not c["wired"]),
                              "wired": sum(1 for c in clients if c["wired"]),
                              "weak": sum(1 for c in clients if isinstance(c["signal"], (int, float))
                                          and c["signal"] < WEAK_SIGNAL)}})
    return out


WEAK_SIGNAL = -75


def all_clients(nets):
    """Jedna lista klientów ze wszystkich routerów i AP. Klient widziany w kilku miejscach (router zna wszystkich
    z DHCP, AP — swoich z Wi-Fi) występuje raz: z tego źródła, które zna połączenie radiowe."""
    out = {}
    for n in nets:
        for c in n["clients"]:
            c = {**c, "via": n["row"]["name"]}
            prev = out.get(c["mac"])
            if not prev or (prev["wired"] and not c["wired"]) or (not prev["ip"] and c["ip"]):
                if prev and not c["ip"]:
                    c["ip"] = prev["ip"]
                out[c["mac"]] = c
    return sorted(out.values(), key=lambda c: (c["status"] != "nowy", _ip_key(c["ip"]), c["name"].lower()))


def _ip_key(ip):
    try:
        return (0, int(ipaddress.ip_address(ip)))
    except ValueError:
        return (1, 0)


# ───────────────────────── Adresy IP ─────────────────────────
def addresses(conn):
    """Sieci /24 wyliczone same z adresów: każda pozycja .1–.254 zajęta (przez co) albo wolna.

    Źródła: adresy urządzeń w dokumentacji (ręczne i z integracji) oraz to, co widzą integracje, a nie jest jeszcze
    przypięte (kolejka „do zatwierdzenia”). Ten sam adres na dwóch urządzeniach = konflikt.
    """
    owners = {}     # ip → [{"device_id"|"ref_id", "name", "kind"}]
    for r in conn.execute(select(device_address.c.value, device.c.id, device.c.name, device.c.kind)
                          .join(device, device.c.id == device_address.c.device_id)
                          .where(device_address.c.kind == "ip")):
        lst = owners.setdefault(r.value, [])
        if not any(o.get("device_id") == r.id for o in lst):
            lst.append({"device_id": r.id, "name": r.name, "kind": "urzadzenie"})
    for r in conn.execute(select(external_ref.c.id, external_ref.c.proposed)
                          .where(external_ref.c.status == "nowy")):
        prop = r.proposed or {}
        for a in prop.get("addresses") or []:
            if a.get("kind") == "ip" and a.get("value"):
                lst = owners.setdefault(a["value"], [])
                if not lst:     # nieprzypisany klient pod adresem, który już ma urządzenie — to samo, nie konflikt
                    lst.append({"ref_id": r.id, "name": prop.get("name") or a["value"], "kind": "nowy"})
    nets = {}
    for ip, own in owners.items():
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # Tylko prywatne IPv4 z LAN-u: adresy Tailscale (100.64/10) przydziela Tailscale, a publiczne — dostawca.
        if addr.version != 4 or not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        nets.setdefault(net, {})[int(addr) - int(net.network_address)] = own
    out = []
    for net, used in sorted(nets.items(), key=lambda kv: int(kv[0].network_address)):
        cells, free = [], []
        for host in range(1, 255):
            own = used.get(host, [])
            devs = [o for o in own if o["kind"] == "urzadzenie"]
            state = "konflikt" if len(devs) > 1 else ("zajety" if devs else ("nowy" if own else "wolny"))
            cells.append({"host": host, "ip": str(net.network_address + host), "state": state, "owners": own})
            if state == "wolny":
                free.append(host)
        out.append({"net": str(net), "prefix": str(net.network_address).rsplit(".", 1)[0], "cells": cells,
                    "used": sum(1 for c in cells if c["state"] != "wolny"), "free": len(free),
                    "free_ranges": _ranges(free), "conflicts": [c for c in cells if c["state"] == "konflikt"]})
    return out


def _ranges(nums):
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
    if start is not None:
        out.append((start, prev))
    return out
