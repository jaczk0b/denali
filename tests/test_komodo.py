"""Adapter Komodo na odpowiedziach w formacie API Komodo (POST /read) i połączenie z VM-kami z Proxmoxa."""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from denali import discovery, integrations, inventory as inv
from denali.adapters import AdapterError
from denali.adapters.komodo import Komodo

CONFIG = {"url": "https://komodo.lan", "api_key": "K-abc", "verify_tls": True}
SECRETS = {"api_secret": "S-tajne"}
SERVERS = [{"id": "srv1", "name": "docker", "info": {"state": "Ok", "address": "https://192.168.1.20:8120"}},
           {"id": "srv2", "name": "stary", "info": {"state": "NotOk", "address": "http://192.168.1.21:8120"}}]
STACKS = [
    {"id": "st1", "name": "immich", "info": {"state": "running", "server_id": "srv1",
                                             "services": [{"service": "immich-server", "update_available": True},
                                                          {"service": "redis", "update_available": False}]}},
    {"id": "st2", "name": "jellyfin", "info": {"state": "unhealthy", "server_id": "srv1", "services": []}},
    {"id": "st3", "name": "stary-stack", "info": {"state": "stopped", "server_id": "srv2"}},
    {"id": "st4", "name": "zepsuty", "info": {"state": "down", "server_id": "srv1"}},
]


def komodo_api(auth_ok=True, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if not auth_ok:
            return httpx.Response(401, json={"error": "invalid api key"})
        body = json.loads(request.content)
        data = {"GetVersion": {"version": "1.18.4"}, "ListServers": SERVERS, "ListStacks": STACKS}.get(body["type"])
        return httpx.Response(200, json=data) if data is not None else httpx.Response(400, json={"error": "?"})
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def test_odczyt_serwerow_i_stackow():
    seen = []
    res = run(Komodo(CONFIG, SECRETS, komodo_api(seen=seen)).poll())
    found = {f.external_id: f.proposed for f in res.found}
    assert found["server/srv1"] == {"name": "docker", "kind": "vm", "addresses": [{"kind": "ip", "value": "192.168.1.20"}]}
    assert found["stack/st1"] == {"name": "immich", "kind": "kontener", "parent": {"ref": "server/srv1"}}
    states = {s.external_id: (s.state, s.summary) for s in res.states}
    assert states["server/srv2"][0] == "down"
    assert states["stack/st1"] == ("ok", "stack działa · usługi: 2 · aktualizacja dostępna: immich-server")
    assert states["stack/st2"][0] == "warn" and states["stack/st3"][0] == "off" and states["stack/st4"][0] == "down"
    assert seen[0].headers["x-api-key"] == "K-abc" and seen[0].headers["x-api-secret"] == "S-tajne"
    assert {json.loads(r.content)["type"] for r in seen} <= {"ListServers", "ListStacks"}     # tylko odczyt


def test_sprawdzenie_polaczenia():
    assert run(Komodo(CONFIG, SECRETS, komodo_api()).test()) == "Połączono: Komodo 1.18.4 · serwery: 2 · stacki: 4"
    with pytest.raises(AdapterError, match="odrzuciło klucz"):
        run(Komodo(CONFIG, SECRETS, komodo_api(auth_ok=False)).test())


def test_stacki_trafiaja_pod_vm_z_proxmoxa(app):
    """VM „docker” z Proxmoxa (192.168.1.20) już jest; serwer Komodo przypina się do niej, stacki pod nią."""
    with app.state.engine.begin() as c:
        pve = inv.create_device(c, "pve", "host")
        vm = inv.create_device(c, "docker", "vm", parent_id=pve)
        inv.add_address(c, vm, "ip", "192.168.1.20")
        iid = integrations.save(c, app.state.box, "komodo", "komodo", CONFIG, SECRETS)
        row = integrations.get(c, iid)
    run(integrations.poll_one(app.state.engine, app.state.box, row, komodo_api(), now=1000))
    with app.state.engine.begin() as c:
        matches = discovery.suggest_matches(c)
        assert [d["id"] for d in matches.values()] == [vm]
        discovery.link_matches(c)
        immich_ref = next(r for r in discovery.pending(c) if r["external_id"] == "stack/st1")
        discovery.approve(c, immich_ref["id"])
        immich = c.execute(select(inv.device).where(inv.device.c.name == "immich")).mappings().first()
        assert immich["parent_id"] == vm and immich["kind"] == "kontener"
        [src] = integrations.states_for_device(c, immich["id"])
        assert src["state"] == "ok" and "aktualizacja dostępna" in src["summary"]
