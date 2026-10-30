"""Stan na żywo i automatyczne incydenty — kryterium Etapu 3 na scenariuszu z makiety (bez powiadomień)."""
import asyncio

from sqlalchemy import select

from denali import checks, discovery, incidents, integrations, inventory as inv, live, profiles
from denali.models import incident

from .test_proxmox import CONFIG, RESOURCES, SECRETS, pve_api


def run(coro):
    return asyncio.run(coro)


def resources_with(**status):
    """RESOURCES z podmienionym stanem maszyn, np. resources_with(**{"100": "stopped"}).
    LXC 110 (w nagraniu adaptera celowo zatrzymany mimo autostartu) tu działa, żeby nie zaciemniał scenariusza."""
    status = {"110": "running", **status}
    out = []
    for r in RESOURCES:
        r = dict(r)
        if str(r.get("vmid")) in status:
            r["status"] = status[str(r["vmid"])]
        out.append(r)
    return out


def setup_lab(app):
    """Proxmox zgłasza węzeł i maszyny; zatwierdzamy wszystko; Immich (stack) zależy od TrueNAS."""
    with app.state.engine.begin() as c:
        iid = integrations.save(c, app.state.box, "proxmox", "pve", CONFIG, SECRETS)
        row = integrations.get(c, iid)
    run(integrations.poll_one(app.state.engine, app.state.box, row, pve_api(resources=resources_with()), now=1000))
    with app.state.engine.begin() as c:
        discovery.approve_all(c)
        ids = {d.name: d.id for d in c.execute(select(inv.device.c.id, inv.device.c.name))}
        ids["immich"] = inv.create_device(c, "Immich", "kontener", parent_id=ids["docker"])
        inv.add_dependency(c, ids["immich"], ids["truenas"], "dane")
        inv.add_address(c, ids["immich"], "ip", "192.168.1.40")
    return iid, ids


def poll(app, iid, resources, now):
    with app.state.engine.begin() as c:
        row = integrations.get(c, iid)
    run(integrations.poll_one(app.state.engine, app.state.box, row, pve_api(resources=resources), now=now))


def ping_result(conn, device_id, ok, now):
    spec = checks.Spec(device_id, "ping", "192.168.1.40")
    checks.record(conn, [(spec, checks.Result(ok, "ping 1 ms" if ok else "192.168.1.40 nie odpowiada"))], [spec], now)


def open_incidents(conn):
    return conn.execute(select(incident).where(incident.c.status != "zamkniety")).mappings().all()


def test_kryterium_etapu(app):
    iid, ids = setup_lab(app)
    # nagranie: VM 100 zatrzymana, ale ma autostart
    from . import test_proxmox as tp
    real = tp.pve_api

    def pve_with_onboot(resources=None, **kw):
        transport = real(resources=resources, **kw)
        inner = transport.handler

        def handler(request):
            if request.url.path.endswith("/nodes/pve/qemu/100/config"):
                import httpx
                return httpx.Response(200, json={"data": {"onboot": 1}})
            return inner(request)
        transport.handler = handler
        return transport

    def poll_onboot(resources, now):
        with app.state.engine.begin() as c:
            row = integrations.get(c, iid)
        run(integrations.poll_one(app.state.engine, app.state.box, row, pve_with_onboot(resources=resources), now=now))

    with app.state.engine.begin() as c:
        ping_result(c, ids["immich"], True, 1000)
        live.evaluate(c, now=1000)

    # 1. TrueNAS staje; Immich przestaje odpowiadać — przez pierwsze 2 minuty to jeszcze nie awaria Immicha
    poll_onboot(resources_with(**{"100": "stopped"}), 1060)
    with app.state.engine.begin() as c:
        ping_result(c, ids["immich"], False, 1060)
        [f] = live.evaluate(c, now=1060)
        assert f.cause == ids["truenas"] and f.effects == []         # Immich jeszcze „czekam”
        ping_result(c, ids["immich"], False, 1200)
        [f] = live.evaluate(c, now=1200)
        assert f.cause == ids["truenas"] and f.effects == [ids["immich"]]
        [inc] = open_incidents(c)                                    # jeden incydent, nie dwa
        detail = incidents.get_incident(c, inc["id"])
        assert detail["title"] == "truenas nie działa"
        assert {(d["name"], d["role"]) for d in detail["devices"]} == {("truenas", "przyczyna"), ("Immich", "skutek")}
        texts = " ".join(e["text"] for e in detail["events"])
        assert "pve działa" in texts and "Przestało też działać: Immich" in texts

    # 2. TrueNAS wraca → incydent zamyka się sam
    poll_onboot(resources_with(), 1300)
    with app.state.engine.begin() as c:
        ping_result(c, ids["immich"], True, 1300)
        assert live.evaluate(c, now=1300) == []
        assert open_incidents(c) == []
        closed = c.execute(select(incident)).mappings().first()
        assert closed["status"] == "zamkniety" and closed["ended_at"] == 1300


def test_wylaczona_celowo_to_nie_awaria(app):
    iid, ids = setup_lab(app)
    poll(app, iid, resources_with(**{"107": "stopped"}), 1060)       # docker zatrzymany, bez autostartu
    with app.state.engine.begin() as c:
        spec = checks.Spec(ids["docker"], "ping", "192.168.1.20")
        checks.record(c, [(spec, checks.Result(False, "nie odpowiada"))], [spec], 500)   # ping milczy od dawna
        assert live.evaluate(c, now=1060) == []
        assert live.saved(c, now=1060)[ids["docker"]]["state"] == "off"


def test_awaria_wyzej_przejmuje_incydent(app):
    iid, ids = setup_lab(app)
    with app.state.engine.begin() as c:
        ping_result(c, ids["immich"], False, 1000)
        live.evaluate(c, now=2000)                                   # Immich nie działa → jego incydent
        [inc] = open_incidents(c)
        assert inc["root_device_id"] == ids["immich"]
    # teraz pada cały węzeł (Proxmox zgłasza węzeł offline)
    nodes_down = [dict(r, status="offline") if r.get("type") == "node" else r for r in resources_with()]
    poll(app, iid, nodes_down, 2100)
    with app.state.engine.begin() as c:
        [f] = live.evaluate(c, now=2100)
        assert f.cause == ids["pve"]
        [inc] = open_incidents(c)
        assert inc["root_device_id"] == ids["pve"]                   # stary incydent zamknięty z odsyłaczem
        old = c.execute(select(incident).where(incident.c.root_device_id == ids["immich"])).mappings().first()
        assert old["status"] == "zamkniety"
        assert "skutek awarii: pve" in incidents.get_incident(c, old["id"])["events"][-1]["text"]


def test_bez_monitorowania_brak_stanu(app):
    iid, ids = setup_lab(app)
    with app.state.engine.begin() as c:
        profiles.set_override(c, ids["truenas"], "monitor", False)
        live.evaluate(c, now=1000)
        assert ids["truenas"] not in live.saved(c, now=1000)
        assert live.saved(c, now=1000 + live.STALE_AFTER + 1) == {}   # stary stan = worker nie działa
