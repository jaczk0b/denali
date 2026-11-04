"""Proxmox: jeden token, zakresy wybierane w panelu, polecenie pveum, sprawdzenie i potwierdzenia hasłem."""
import asyncio
import re
import shlex

import httpx
import pytest

from denali import actions, integrations
from denali.adapters import AdapterError
from denali.adapters.proxmox import Proxmox

from .conftest import csrf_from
from .test_akcje import admin, audit_rows, fresh_reauth, pve  # noqa: F401
from .test_proxmox import CONFIG, SECRETS, pve_api


def run(coro):
    return asyncio.run(coro)


def with_permissions(privs):
    """API Proxmoxa z odczytu + /access/permissions z podanymi uprawnieniami na /vms (None = błąd)."""
    base = pve_api()

    def handler(request):
        if request.url.path.endswith("/access/permissions"):
            assert request.url.params["path"] == "/vms"
            if privs is None:
                return httpx.Response(500)
            return httpx.Response(200, json={"data": {"/vms": {p: 1 for p in privs}}})
        return base.handle_request(request)
    return httpx.MockTransport(handler)


def test_polecenie_nowej_integracji():
    text = Proxmox.setup_command(["akcje", "konsola"])
    assert 'pveum role add DenaliPanel --privs "VM.PowerMgmt,VM.Snapshot,VM.Console"' in text
    assert 'pveum role modify DenaliPanel --privs "VM.PowerMgmt,VM.Snapshot,VM.Console"' in text
    assert "pveum acl modify / --users denali@pve --roles PVEAuditor" in text
    assert "pveum acl modify /vms --users denali@pve --roles DenaliPanel" in text
    assert "pveum user token add denali@pve panel --privsep 0" in text
    assert text.endswith("cat /etc/pve/pve-root-ca.pem")


def test_polecenie_tylko_odczyt_nie_ma_wlasnej_roli():
    text = Proxmox.setup_command([])
    assert "DenaliPanel" not in text and "PVEAuditor" in text and "token add" in text


def test_polecenie_dla_zapisanego_tokenu():
    """Token już jest: nie zakładamy nowego, a uprawnienia dostają i użytkownik, i token (także z oddzielonymi)."""
    text = Proxmox.setup_command(["konsola"], "denali@pve!odczyt")
    assert "token add" not in text and "cat /etc/pve" not in text
    assert 'pveum role add DenaliPanel --privs "VM.Console"' in text and "VM.Snapshot" not in text
    assert "pveum acl modify /vms --users denali@pve --tokens 'denali@pve!odczyt' --roles DenaliPanel" in text
    # odebranie wszystkiego
    assert "pveum acl delete /vms --users denali@pve --tokens 'denali@pve!odczyt' --roles DenaliPanel" in \
        Proxmox.setup_command([], "denali@pve!odczyt")


def test_polecenie_nie_przepuszcza_powloki():
    text = Proxmox.setup_command(["akcje"], "x@pve!a;reboot")
    assert "--tokens 'x@pve!a;reboot'" in text
    assert all(shlex.split(line) for line in text.splitlines())


def test_po_jednym_poleceniu_na_uklad_pol():
    assert set(Proxmox.setup_commands()) == {"", "akcje", "konsola", "akcje,konsola"}


def test_sprawdzenie_mowi_czego_brakuje():
    config = {**CONFIG, "permissions": ["akcje", "konsola"]}
    ok = Proxmox(config, SECRETS, transport=with_permissions(["VM.PowerMgmt", "VM.Snapshot", "VM.Console"]))
    assert run(ok.test()).startswith("Połączono")
    bad = Proxmox(config, SECRETS, transport=with_permissions(["VM.PowerMgmt", "VM.Snapshot"]))
    with pytest.raises(AdapterError, match="VM.Console"):
        run(bad.test())
    # sam odczyt niczego nie wymaga, a brak odpowiedzi o uprawnieniach nie blokuje połączenia
    assert run(Proxmox(CONFIG, SECRETS, transport=with_permissions([])).test()).startswith("Połączono")
    assert run(Proxmox(config, SECRETS, transport=with_permissions(None)).test()).startswith("Połączono")


def test_nowa_integracja_z_zakresami(app, admin):
    html = admin.get("/admin/integracje/nowa?typ=proxmox").text
    assert 'value="denali@pve!panel"' in html
    commands = re.findall(r'<pre class="wrap mono small" data-command="([^"]*)" (hidden)?', html)
    assert sorted(k for k, _h in commands) == ["", "akcje", "akcje,konsola", "konsola"]
    assert [k for k, h in commands if not h] == [""]          # widać tylko polecenie dla tego, co zaznaczone
    assert "Datacenter → Permissions" not in html
    r = admin.post("/admin/integracje/nowa", data={
        "csrf_token": csrf_from(html), "type": "proxmox", "name": "pve", "url": CONFIG["url"], "token_id": "denali@pve!panel",
        "token_secret": "s", "verify_tls": "1", "grant": ["konsola", "akcje", "smiec"]})
    assert r.status_code == 303
    with app.state.engine.connect() as conn:
        row = integrations.get(conn, int(r.headers["location"].split("/")[3].split("?")[0]))
    assert row["config"]["permissions"] == ["akcje", "konsola"]


def test_dodanie_zakresu_wymaga_potwierdzenia_a_odebranie_nie(app, pve, admin):
    url = f"/admin/integracje/{pve['iid']}/uprawnienia"
    page = admin.get(f"/admin/integracje/{pve['iid']}").text
    assert 'id="uprawnienia"' in page and "--tokens &#39;denali@pve!odczyt&#39;" in page
    assert "Akcje — osobny token" not in page
    csrf = csrf_from(page)
    r = admin.post(url, data={"csrf_token": csrf, "grant": ["akcje", "konsola"]})
    assert r.headers["location"].startswith("/potwierdz")

    def chosen():
        with app.state.engine.connect() as conn:
            return integrations.get(conn, pve["iid"])["config"]["permissions"]

    assert chosen() == ["akcje"]
    assert admin.post(url, data={"csrf_token": csrf}).status_code == 303
    assert chosen() == []
    fresh_reauth(app, admin)
    admin.post(url, data={"csrf_token": csrf, "grant": ["akcje", "konsola"]})
    assert chosen() == ["akcje", "konsola"]
    with app.state.engine.connect() as conn:
        assert [a["params"]["permissions"] for a in audit_rows(conn, "integration_grants")][-2:] == [[], ["akcje", "konsola"]]


def test_zapis_ustawien_nie_rusza_zakresow(app, pve, admin):
    page = admin.get(f"/admin/integracje/{pve['iid']}").text
    r = admin.post(f"/admin/integracje/{pve['iid']}", data={
        "csrf_token": csrf_from(page), "type": "proxmox", "name": "nowa nazwa", "url": CONFIG["url"],
        "token_id": CONFIG["token_id"], "verify_tls": "1", "grant": ["konsola"]})
    assert r.status_code == 303
    with app.state.engine.connect() as conn:
        row = integrations.get(conn, pve["iid"])
    assert row["name"] == "nowa nazwa" and row["config"]["permissions"] == ["akcje"]


def test_token_z_zakresem_nie_pojdzie_na_inny_adres_bez_potwierdzenia(app, pve, admin):
    page = admin.get(f"/admin/integracje/{pve['iid']}").text
    data = {"csrf_token": csrf_from(page), "type": "proxmox", "name": "pve", "url": "https://zly.host:8006",
            "token_id": CONFIG["token_id"], "verify_tls": "1"}
    r = admin.post(f"/admin/integracje/{pve['iid']}", data=data)
    assert r.status_code == 303 and r.headers["location"].startswith("/potwierdz")
    with app.state.engine.connect() as conn:
        assert integrations.get(conn, pve["iid"])["config"]["url"] == CONFIG["url"]
    fresh_reauth(app, admin)
    assert admin.post(f"/admin/integracje/{pve['iid']}", data=data).status_code == 303
    with app.state.engine.connect() as conn:
        assert integrations.get(conn, pve["iid"])["config"]["url"] == "https://zly.host:8006"


def test_zapis_zakresow_usuwa_stary_token_do_akcji(app, pve):
    with app.state.engine.begin() as conn:
        conn.execute(integrations.integration.update().values(action_ct=app.state.box.encrypt("{}")))
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["akcje"], "admin")
        assert integrations.get(conn, pve["iid"])["action_ct"] is None


def test_komodo_ma_dalej_osobny_klucz(app):
    from denali.adapters.komodo import Komodo
    assert Komodo.action_fields and not Komodo.grants
