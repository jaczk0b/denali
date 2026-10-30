"""PBS i TrueNAS: odczyt, podpinanie stanu pod istniejące urządzenia (kopie pod VM, pule pod TrueNAS)."""
import asyncio
import json
import time

import httpx
import pytest

from denali import discovery, integrations, inventory as inv, live
from denali.adapters import AdapterError
from denali.adapters.pbs import Pbs
from denali.adapters.truenas import Truenas

from .test_proxmox import CONFIG as PVE_CONFIG, SECRETS as PVE_SECRETS, pve_api

NOW = time.time()
PBS_CONFIG = {"url": "https://192.168.1.15:8007", "token_id": "denali@pbs!odczyt", "verify_tls": True, "ca_pem": ""}
PBS_SECRETS = {"token_secret": "pbs-sekret"}


def run(coro):
    return asyncio.run(coro)


def pbs_api(seen=None):
    groups = {
        "main": [{"backup-type": "vm", "backup-id": "100", "last-backup": NOW - 3600, "backup-count": 14},
                 {"backup-type": "ct", "backup-id": "110", "last-backup": NOW - 5 * 86400, "backup-count": 3},
                 {"backup-type": "host", "backup-id": "laptop", "last-backup": NOW, "backup-count": 1}],
        "offsite": [{"backup-type": "vm", "backup-id": "100", "last-backup": NOW - 86400, "backup-count": 30}],
    }

    def handler(request):
        if seen is not None:
            seen.append(request)
        path = request.url.path.removeprefix("/api2/json")
        if path == "/admin/datastore":
            return httpx.Response(200, json={"data": [{"store": "main"}, {"store": "offsite"}]})
        if path == "/status/datastore-usage":
            return httpx.Response(200, json={"data": [{"store": "main", "total": 100, "used": 90},
                                                      {"store": "offsite", "total": 100, "used": 40}]})
        if path.startswith("/admin/datastore/") and path.endswith("/groups"):
            return httpx.Response(200, json={"data": groups[path.split("/")[3]]})
        return httpx.Response(404, json={"data": None})
    return httpx.MockTransport(handler)


def test_pbs_ostatnia_kopia_i_magazyny():
    seen = []
    res = run(Pbs(PBS_CONFIG, PBS_SECRETS, pbs_api(seen)).poll())
    st = {s.external_id: s for s in res.states}
    assert st["server"].state == "warn" and "main 90%" in st["server"].summary          # magazyn prawie pełny
    assert st["backup/vm/100"].state == "ok" and "kopii: 14 · magazyn main" in st["backup/vm/100"].summary
    assert st["backup/vm/100"].attach == {"integration_type": "proxmox", "external_ids": ["qemu/100"]}
    assert st["backup/ct/110"].state == "warn" and "brak świeżej kopii" in st["backup/ct/110"].summary
    assert "backup/host/laptop" not in st                                                 # kopie hostów pomijamy
    assert seen[0].headers["authorization"] == "PBSAPIToken=denali@pbs!odczyt:pbs-sekret"
    assert run(Pbs(PBS_CONFIG, PBS_SECRETS, pbs_api()).test()) == "Połączono: magazyny: 2 · kopie VM i kontenerów: 3"


def test_kopie_podpinaja_sie_pod_vm_z_proxmoxa(app):
    with app.state.engine.begin() as c:
        pve_id = integrations.save(c, app.state.box, "proxmox", "pve", PVE_CONFIG, PVE_SECRETS)
        pbs_id = integrations.save(c, app.state.box, "pbs", "pbs", PBS_CONFIG, PBS_SECRETS)
        pve_row, pbs_row = integrations.get(c, pve_id), integrations.get(c, pbs_id)
    run(integrations.poll_one(app.state.engine, app.state.box, pve_row, pve_api(), now=1000))
    run(integrations.poll_one(app.state.engine, app.state.box, pbs_row, pbs_api(), now=1000))
    with app.state.engine.begin() as c:
        truenas_ref = next(r for r in discovery.pending(c) if r["external_id"] == "qemu/100")
        assert all(r["external_id"] != "backup/vm/100" for r in discovery.pending(c))     # kopia to nie urządzenie
        dev = [d for d in discovery.approve(c, truenas_ref["id"]) if inv.get_device(c, d)["name"] == "truenas"][0]
        assert [s["proposed"]["name"] for s in integrations.states_for_device(c, dev)
                if s["source"] == "pbs"] == []                                            # jeszcze nie podpięte
    run(integrations.poll_one(app.state.engine, app.state.box, pbs_row, pbs_api(), now=1100))
    with app.state.engine.begin() as c:
        [backup] = [s for s in integrations.states_for_device(c, dev) if s["source"] == "pbs"]
        assert backup["proposed"]["name"] == "kopie VM 100" and backup["state"] == "ok"


# ───────────────────────── TrueNAS ─────────────────────────
TN_CONFIG = {"url": "https://192.168.1.30", "verify_tls": False, "ca_pem": ""}
TN_SECRETS = {"api_key": "1-tajnyklucz"}
TN_DATA = {
    "system.info": {"version": "TrueNAS-SCALE-24.10.2", "hostname": "truenas"},
    "pool.query": [{"name": "tank", "status": "ONLINE", "healthy": True, "size": 1000, "allocated": 630},
                   {"name": "kopie", "status": "DEGRADED", "healthy": False, "size": 100, "allocated": 10}],
    "pool.snapshottask.query": [
        {"dataset": "tank/zdjecia", "enabled": True, "state": {"state": "FINISHED", "datetime": {"$date": NOW * 1000}}},
        {"dataset": "tank/filmy", "enabled": True, "state": {"state": "ERROR", "error": "dataset is busy"}},
        {"dataset": "tank/stare", "enabled": False, "state": {}}],
    "replication.query": [],
    "alert.list": [{"level": "WARNING", "formatted": "drobiazg", "dismissed": False}],
    "sharing.nfs.query": [{"path": "/mnt/tank/zdjecia"}],
}
REST_PATHS = {"/system/info": "system.info", "/pool": "pool.query", "/pool/snapshottask": "pool.snapshottask.query",
              "/replication": "replication.query", "/alert/list": "alert.list", "/sharing/nfs": "sharing.nfs.query"}


def truenas_rest(seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        method = REST_PATHS.get(request.url.path.removeprefix("/api/v2.0"))
        return httpx.Response(200, json=TN_DATA[method]) if method else httpx.Response(404)
    return httpx.MockTransport(handler)


def check_truenas_result(res):
    found = {f.external_id: f.proposed for f in res.found}
    assert found["server"] == {"name": "truenas", "kind": "nas", "addresses": [{"kind": "ip", "value": "192.168.1.30"}]}
    st = {s.external_id: s for s in res.states}
    assert st["server"].state == "ok"                                          # samo WARNING nie robi alarmu
    assert st["pool/tank"].state == "ok" and st["pool/tank"].summary == "pula tank: ONLINE · zajęte 63%"
    assert st["pool/kopie"].state == "warn"
    assert st["snapshot/tank/zdjecia"].state == "ok" and st["snapshot/tank/filmy"].state == "warn"
    assert "snapshot/tank/stare" not in st                                     # wyłączone zadanie
    assert all(s.attach == {"ref": "server"} for k, s in st.items() if k != "server")


def test_truenas_rest():
    seen = []
    check_truenas_result(run(Truenas(TN_CONFIG, TN_SECRETS, truenas_rest(seen)).poll()))
    assert seen[0].headers["authorization"] == "Bearer 1-tajnyklucz"
    assert all(r.method == "GET" for r in seen)


def test_truenas_jsonrpc_po_websocket():
    """Prawdziwy serwer WebSocket w teście — tak rozmawia TrueNAS 25.04+ (/api/current)."""
    import websockets

    calls = []

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            calls.append(msg["method"])
            if msg["method"] == "auth.login_with_api_key":
                result = msg["params"] == ["1-tajnyklucz"]
            elif msg["method"] in TN_DATA:
                result = TN_DATA[msg["method"]]
            else:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"message": "Method not found"}}))
                continue
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}))

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            adapter = Truenas({**TN_CONFIG, "url": f"http://127.0.0.1:{port}"}, TN_SECRETS)
            return await adapter.poll(), await Truenas({**TN_CONFIG, "url": f"http://127.0.0.1:{port}"},
                                                       {"api_key": "zly"}).test()

    with pytest.raises(AdapterError, match="odrzucił klucz"):
        run(scenario())
    assert calls[0] == "auth.login_with_api_key" and "pool.query" in calls


def test_truenas_jsonrpc_wynik():
    import websockets

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            result = True if msg["method"] == "auth.login_with_api_key" else TN_DATA.get(msg["method"])
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}))

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            return await Truenas({**TN_CONFIG, "url": f"http://127.0.0.1:{port}"}, TN_SECRETS).poll()

    res = run(scenario())
    st = {s.external_id: s for s in res.states}
    assert st["pool/tank"].state == "ok" and st["pool/kopie"].state == "warn"
    assert {f.proposed["name"] for f in res.found} == {"truenas"}


def test_pula_podpina_sie_pod_karte_truenas_i_zmienia_stan(app):
    with app.state.engine.begin() as c:
        vm = inv.create_device(c, "truenas", "vm")
        inv.add_address(c, vm, "ip", "192.168.1.30")
        iid = integrations.save(c, app.state.box, "truenas", "truenas", TN_CONFIG, TN_SECRETS)
        row = integrations.get(c, iid)
    run(integrations.poll_one(app.state.engine, app.state.box, row, truenas_rest(), now=1000))
    with app.state.engine.begin() as c:
        assert discovery.link_matches(c) == 1                   # serwer TrueNAS = istniejąca VM (po IP)
    run(integrations.poll_one(app.state.engine, app.state.box, row, truenas_rest(), now=1100))
    with app.state.engine.begin() as c:
        names = {s["proposed"]["name"] for s in integrations.states_for_device(c, vm)}
        assert {"pula tank", "pula kopie", "snapshoty tank/zdjecia"} <= names
        live.evaluate(c, now=1100)
        assert live.saved(c, now=1100)[vm]["state"] == "warn"   # zdegradowana pula = uwaga, nie awaria
