"""Jedna lista aktualizacji: Proxmox (pakiety węzła), PBS, Komodo (nowsze obrazy) — zapis, widok, licznik, profil."""
import asyncio

import httpx
from sqlalchemy import select

from denali import discovery, integrations, profiles, updates
from denali.adapters import apt_updates
from denali.adapters.komodo import Komodo
from denali.adapters.pbs import Pbs
from denali.adapters.proxmox import Proxmox
from denali.models import update_item

from .conftest import do_login, enable_2fa, make_user, new_client
from .test_komodo import CONFIG as KOMODO_CONFIG, SECRETS as KOMODO_SECRETS, komodo_api
from .test_pbs_truenas import PBS_CONFIG, PBS_SECRETS, pbs_api
from .test_proxmox import CONFIG, SECRETS, pve_api

# format odpowiedzi /nodes/<węzeł>/apt/update (Proxmox VE 8 i PBS 3)
APT = [
    {"Package": "pve-manager", "OldVersion": "8.4.1", "Version": "8.4.5", "Origin": "Proxmox", "Section": "admin",
     "Title": "Proxmox Virtual Environment Management Tools"},
    {"Package": "openssl", "OldVersion": "3.0.15-1~deb12u1", "Version": "3.0.16-1~deb12u1",
     "Origin": "Debian", "Section": "utils", "Priority": "optional"},
    {"Package": "libc6", "OldVersion": "2.36-9+deb12u9", "Version": "2.36-9+deb12u10",
     "Origin": "Debian-Security", "Section": "libs"},
]


def run(coro):
    return asyncio.run(coro)


def with_apt(inner, path, packages, status=200):
    def handler(request):
        if request.url.path.removeprefix("/api2/json") == path:
            return httpx.Response(status, json={"data": packages if status == 200 else None})
        return inner.handle_request(request)
    return httpx.MockTransport(handler)


def test_lista_pakietow_z_apt():
    us = apt_updates("node/pve", APT + [{"Title": "bez nazwy"}, "śmieć"])
    assert [(u.component, u.current, u.available, u.security) for u in us] == [
        ("pve-manager", "8.4.1", "8.4.5", False), ("openssl", "3.0.15-1~deb12u1", "3.0.16-1~deb12u1", False),
        ("libc6", "2.36-9+deb12u9", "2.36-9+deb12u10", True)]


def test_zrodla_podaja_aktualizacje():
    res = run(Proxmox(CONFIG, SECRETS, with_apt(pve_api(), "/nodes/pve/apt/update", APT)).poll())
    assert {u.external_id for u in res.updates} == {"node/pve"} and len(res.updates) == 3
    # brak uprawnień do listy → None: poprzednia lista zostaje, a nie znika
    assert run(Proxmox(CONFIG, SECRETS, with_apt(pve_api(), "/nodes/pve/apt/update", None, 403)).poll()).updates is None

    res = run(Pbs(PBS_CONFIG, PBS_SECRETS, with_apt(pbs_api(), "/nodes/localhost/apt/update", APT[:1])).poll())
    assert [(u.external_id, u.component) for u in res.updates] == [("server", "pve-manager")]
    assert run(Pbs(PBS_CONFIG, PBS_SECRETS, pbs_api()).poll()).updates is None

    res = run(Komodo(KOMODO_CONFIG, KOMODO_SECRETS, komodo_api()).poll())
    assert [(u.external_id, u.component, u.kind) for u in res.updates] == [("stack/st1", "immich-server", "obraz")]


def test_jedna_lista_licznik_i_profil(app):
    make_user(app)
    with app.state.engine.begin() as c:
        pid = integrations.save(c, app.state.box, "proxmox", "pve", CONFIG, SECRETS)
        kid = integrations.save(c, app.state.box, "komodo", "komodo", KOMODO_CONFIG, KOMODO_SECRETS)

    def poll(iid, transport, now):
        with app.state.engine.begin() as c:
            row = integrations.get(c, iid)
        assert run(integrations.poll_one(app.state.engine, app.state.box, row, transport, now=now)) is None

    poll(pid, with_apt(pve_api(), "/nodes/pve/apt/update", APT), 1000)
    poll(kid, komodo_api(), 1000)
    with app.state.engine.begin() as c:
        assert updates.overview(c) == []                         # niezatwierdzone obiekty nie trafiają na listę
        discovery.approve_all(c)
        rows = {r["device"].name: r for r in updates.overview(c)}
        assert set(rows) == {"pve", "immich"}
        assert rows["pve"]["security"] == 1 and len(rows["pve"]["rows"]) == 3
        assert list(rows)[0] == "pve"                            # najpierw poprawki bezpieczeństwa
        assert updates.count(c) == 2
        immich = rows["immich"]["device"].id

    # kolejny odczyt: pakiet zaktualizowany znika, „od kiedy” zostaje dla tej samej wersji
    poll(pid, with_apt(pve_api(), "/nodes/pve/apt/update", APT[1:]), 2000)
    with app.state.engine.begin() as c:
        got = {r.component: r.detected_at for r in c.execute(select(update_item).where(update_item.c.integration_id == pid))}
        assert got == {"openssl": 1000, "libc6": 1000}
        # profil „ignoruj”: zostaje na liście, ale nie w liczniku
        profiles.set_override(c, immich, "updates", "ignoruj")
        assert updates.count(c) == 1
        assert [r["device"].name for r in updates.overview(c)] == ["pve", "immich"]

    with new_client(app) as cl:
        do_login(cl)
        enable_2fa(cl, app)
        page = cl.get("/aktualizacje").text
        assert "libc6" in page and "2.36-9+deb12u9 → 2.36-9+deb12u10" in page and "bezpieczeństwo: 1" in page
        assert "nowszy obraz" in page and "profil: ignoruj" in page
        assert 'href="/aktualizacje"' in page and '<span class="badge">1</span>' in page
        with app.state.engine.connect() as c:
            pve = next(r["device"].id for r in updates.overview(c) if r["device"].name == "pve")
        card = cl.get(f"/urzadzenia/{pve}").text
        assert 'id="aktualizacje"' in card and "openssl" in card
