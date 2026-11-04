"""Akcje (Etap 5): zamki (wyłącznik, lista dozwolonych, token do akcji, stan, potwierdzenie hasłem), wykonanie przez
API Proxmoxa i Komodo, audyt i przebieg incydentu, łańcuch „snapshot → apt upgrade” przez agenta, agent na maszynie."""
import asyncio
import json
import re

import httpx
import pytest
from sqlalchemy import select

from denali import actions, agents, auth, discovery, incidents, integrations, profiles
from denali.adapters import AdapterError
from denali.adapters.komodo import Komodo
from denali.adapters.proxmox import Proxmox
from denali.models import action_run, audit_log, external_ref, incident_event

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client
from .test_agent import agent, fake_report
from .test_proxmox import CONFIG, SECRETS, pve_api

UPID = "UPID:pve:0001:0002:6720AB00:qmreboot:107:denali@pve!akcje:"


def run(coro):
    return asyncio.run(coro)


def pve_actions(seen=None, task_status=None, deny=False):
    """API Proxmoxa: odczyt z test_proxmox + akcje (POST status/…, snapshot) i stan zadań."""
    base = pve_api()
    task_status = task_status or {"status": "stopped", "exitstatus": "OK"}

    def handler(request):
        path = request.url.path.removeprefix("/api2/json")
        if request.method == "POST" or "/tasks/" in path:
            if seen is not None:
                seen.append(request)
            if deny:
                return httpx.Response(403, json={"data": None})
        if request.method == "POST" and re.fullmatch(r"/nodes/pve/(qemu|lxc)/\d+/(status/\w+|snapshot)", path):
            return httpx.Response(200, json={"data": UPID})
        if request.method == "GET" and path.startswith("/nodes/pve/tasks/") and path.endswith("/status"):
            return httpx.Response(200, json={"data": task_status})
        return base.handle_request(request)
    return httpx.MockTransport(handler)


@pytest.fixture
def pve(app):
    """Proxmox z tokenem do akcji, zatwierdzone VM 107 (działa) i 120 (wyłączona)."""
    with app.state.engine.begin() as conn:
        iid = integrations.save(conn, app.state.box, "proxmox", "pve", dict(CONFIG), dict(SECRETS))
        row = integrations.get(conn, iid)
    assert run(integrations.poll_one(app.state.engine, app.state.box, row, pve_api())) is None
    with app.state.engine.begin() as conn:
        refs = {r["external_id"]: r for r in discovery.pending(conn)}
        discovery.approve(conn, refs["qemu/107"]["id"])
        discovery.approve(conn, refs["qemu/120"]["id"])
        ids = {r.external_id: (r.id, r.device_id) for r in conn.execute(select(external_ref))}
        actions.save_grants(conn, integrations.get(conn, iid), ["akcje"], "admin")
    return {"iid": iid, "docker": ids["qemu/107"], "test": ids["qemu/120"]}


def allow(conn, keys, kind="vm"):
    """Dozwolone akcje w profilu, który dostają VM-ki."""
    p = profiles.profile_for(conn, {"profile_id": None, "kind": kind})
    profiles.set_profile_setting(conn, p["id"], "actions", keys)


def req(conn, target, key, **kw):
    ref_id, device_id = target
    return actions.request(conn, device_id=device_id, ref_id=ref_id, key=key, user="admin",
                           role=kw.pop("role", "admin"), ip="192.168.1.5", **kw)


def audit_rows(conn, action):
    return conn.execute(select(audit_log).where(audit_log.c.action == action)).mappings().all()


# ───────────────────────── zamki ─────────────────────────
def test_akcja_spoza_listy_jest_niemozliwa(app, pve):
    with app.state.engine.begin() as conn:
        opts = {o["key"]: o for o in actions.for_device(conn, pve["docker"][1])}
        assert set(opts) == {"proxmox.start", "proxmox.shutdown", "proxmox.reboot", "proxmox.stop", "proxmox.snapshot"}
        assert all("Niedozwolona" in o["reason"] for o in opts.values())
        with pytest.raises(actions.ActionError, match="Niedozwolona"):
            req(conn, pve["docker"], "proxmox.snapshot")
        with pytest.raises(actions.ActionError, match="Tej akcji nie ma"):
            req(conn, pve["docker"], "proxmox.formatuj")          # klucz spoza kodu
        assert len(audit_rows(conn, "action_blocked")) == 2
        assert not conn.execute(select(action_run)).first()


def test_kolejne_zamki(app, pve):
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.start", "proxmox.reboot", "proxmox.snapshot"])
        # podgląd nie zleca niczego
        with pytest.raises(actions.ActionError, match="tylko administrator"):
            req(conn, pve["docker"], "proxmox.snapshot", role="podglad")
        # niebezpieczna bez świeżego potwierdzenia hasłem i kodem
        with pytest.raises(actions.ActionError, match="hasłem"):
            req(conn, pve["docker"], "proxmox.reboot")
        # start działającej VM nie ma sensu
        with pytest.raises(actions.ActionError, match="stanie"):
            req(conn, pve["docker"], "proxmox.start")
        # wyłącznik
        actions.set_enabled(conn, False, "admin")
        with pytest.raises(actions.ActionError, match="wyłączone"):
            req(conn, pve["docker"], "proxmox.snapshot")
        actions.set_enabled(conn, True, "admin")
        # integracja bez zakresu „Akcje”
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["konsola"], "admin")
        with pytest.raises(actions.ActionError, match="zakresu „Akcje”"):
            req(conn, pve["docker"], "proxmox.snapshot")
        actions.save_grants(conn, integrations.get(conn, pve["iid"]), ["akcje"], "admin")
        # wyjątek na urządzeniu zabiera akcję tylko temu urządzeniu
        profiles.set_override(conn, pve["docker"][1], "actions", ["proxmox.start"])
        with pytest.raises(actions.ActionError, match="Niedozwolona"):
            req(conn, pve["docker"], "proxmox.snapshot")
        assert req(conn, pve["test"], "proxmox.snapshot")
        # na obiekcie trwa już akcja
        with pytest.raises(actions.ActionError, match="trwa inna"):
            req(conn, pve["test"], "proxmox.snapshot")
        assert len(audit_rows(conn, "action_blocked")) == 7


def test_nieznany_klucz_w_profilu_odrzucony(conn):
    with pytest.raises(profiles.ProfileError, match="Nieznana akcja"):
        profiles.validate("actions", ["proxmox.formatuj"])


# ───────────────────────── wykonanie ─────────────────────────
def test_restart_vm_przez_proxmoxa_z_audytem_i_incydentem(app, pve):
    seen = []
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.reboot"])
        iid = incidents.open_manual(conn, "docker nie odpowiada", "admin", device_id=pve["docker"][1])
        run_id = req(conn, pve["docker"], "proxmox.reboot", reauth_ok=True)
    run(actions.execute(app.state.engine, app.state.box, run_id, pve_actions(seen), wait=0.1))
    with app.state.engine.connect() as conn:
        r = actions.get(conn, run_id)
        assert r["status"] == "ok" and r["requested_by"] == "admin" and r["task"]["upid"] == UPID
        assert [(x.method, x.url.path) for x in seen] == [
            ("POST", "/api2/json/nodes/pve/qemu/107/status/reboot"),
            ("GET", f"/api2/json/nodes/pve/tasks/{UPID}/status")]
        assert seen[0].headers["authorization"] == "PVEAPIToken=denali@pve!odczyt=11111111-2222-3333-4444-555555555555"   # ten sam token co odczyt
        [requested] = audit_rows(conn, "action_request")
        assert requested["params"]["action"] == "proxmox.reboot" and requested["ip"] == "192.168.1.5"
        [result] = audit_rows(conn, "action_result")
        assert result["result"].startswith("ok") and result["user"] == "admin"
        events = [e.text for e in conn.execute(select(incident_event).where(incident_event.c.incident_id == iid,
                                                                            incident_event.c.kind == "akcja"))]
        assert events[0].startswith("Akcja zlecona: Uruchom ponownie") and events[1].startswith("Akcja wykonana")
        # źródło odczytane ponownie przy najbliższym obiegu
        assert integrations.get(conn, pve["iid"])["last_attempt_at"] is None


def test_blad_zrodla_i_dlugie_zadanie(app, pve):
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.snapshot"])
        bad = req(conn, pve["docker"], "proxmox.snapshot")
    run(actions.execute(app.state.engine, app.state.box, bad, pve_actions(deny=True), wait=0))
    with app.state.engine.begin() as conn:
        r = actions.get(conn, bad)
        assert r["status"] == "blad" and "Uprawnienia" in r["result"]
        assert "błąd" in audit_rows(conn, "action_result")[0]["result"]
        slow = req(conn, pve["docker"], "proxmox.snapshot")
    run(actions.execute(app.state.engine, app.state.box, slow, pve_actions(task_status={"status": "running"}), wait=0))
    with app.state.engine.connect() as conn:
        assert actions.get(conn, slow)["status"] == "trwa"
    # worker dokańcza zadanie, gdy Proxmox je skończy
    run(actions.follow(app.state.engine, app.state.box, pve_actions()))
    with app.state.engine.connect() as conn:
        r = actions.get(conn, slow)
        assert r["status"] == "ok" and r["result"].startswith("snapshot denali-")


def test_zlecenie_wykonuje_sie_raz(app, pve):
    seen = []
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.snapshot"])
        run_id = req(conn, pve["docker"], "proxmox.snapshot")
    run(actions.execute(app.state.engine, app.state.box, run_id, pve_actions(seen), wait=0))
    run(actions.execute(app.state.engine, app.state.box, run_id, pve_actions(seen), wait=0))
    run(actions.follow(app.state.engine, app.state.box, pve_actions(seen)))
    assert sum(1 for x in seen if x.method == "POST") == 1


def test_proxmox_adapter_akcje():
    seen = []
    a = Proxmox(CONFIG, SECRETS, pve_actions(seen))
    res = run(a.run_action("snapshot", "lxc/110", {"node": "pve"}, {"snapname": "przed-aktualizacja"}))
    assert res.ok and res.task["upid"] == UPID
    assert seen[0].url.path.endswith("/lxc/110/snapshot") and b"snapname=przed-aktualizacja" in seen[0].content
    with pytest.raises(AdapterError, match="węźle"):
        run(a.run_action("start", "qemu/107", {}, {}))
    with pytest.raises(AdapterError, match="Zła nazwa"):
        run(a.run_action("snapshot", "qemu/107", {"node": "pve"}, {"snapname": "x; rm"}))
    failed = Proxmox(CONFIG, SECRETS, pve_actions(task_status={"status": "stopped", "exitstatus": "VM is locked"}))
    assert run(failed.check_task(res.task)).ok is False


def komodo_exec(seen):
    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body, request.headers["x-api-key"]))
        if request.url.path == "/execute":
            return httpx.Response(200, json={"_id": {"$oid": "u123"}, "status": "Queued"})
        if body["type"] == "GetUpdate":
            return httpx.Response(200, json={"status": "Complete", "success": False, "logs": [
                {"stage": "compose pull", "success": True, "stdout": "ok"},
                {"stage": "compose up", "success": False, "stderr": "port 8080 already allocated"}]})
        return httpx.Response(400, json={"error": "?"})
    return httpx.MockTransport(handler)


def test_komodo_adapter_akcje():
    seen = []
    a = Komodo({"url": "https://komodo.lan", "api_key": "K-odczyt"}, {"api_secret": "s"}, komodo_exec(seen),
               {"action_api_key": "K-akcje", "action_api_secret": "s2"})
    res = run(a.run_action("deploy", "stack/st1", {}, {}))
    assert res.task == {"update": "u123"}
    assert seen[0] == ("/execute", {"type": "DeployStack", "params": {"stack": "st1"}}, "K-akcje")
    done = run(a.check_task(res.task))
    assert done.ok is False and "port 8080 already allocated" in done.text
    with pytest.raises(AdapterError, match="Nieznana"):
        run(a.run_action("deploy", "server/srv1", {}, {}))


# ───────────────────────── panel ─────────────────────────
@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


def fresh_reauth(app, client):
    with app.state.engine.begin() as conn:
        auth.mark_reauth(conn, client.cookies.get("denali_session"))


def test_panel_potwierdzenie_i_wykonanie(app, pve, admin):
    app.state.adapter_transport = pve_actions()
    ref_id, dev = pve["docker"]
    url = f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.reboot"
    card = admin.get(f"/urzadzenia/{dev}").text
    assert 'id="akcje"' in card and "Niedozwolona" in card and url not in card
    # bezpośredni POST z pominięciem przycisku — ta sama odmowa
    r = admin.post(url, data={"csrf_token": csrf_from(card)})
    assert r.status_code == 403 and "Niedozwolona" in r.text
    # lista dozwolonych w profilu wymaga potwierdzenia hasłem
    page = admin.get("/admin/profile").text
    assert admin.post("/admin/profile/1/akcje", data={"csrf_token": csrf_from(page), "allow": "proxmox.reboot"}
                      ).headers["location"].startswith("/potwierdz")
    fresh_reauth(app, admin)
    with app.state.engine.begin() as conn:
        vm_profile = profiles.profile_for(conn, {"profile_id": None, "kind": "vm"})
    r = admin.post(f"/admin/profile/{vm_profile['id']}/akcje", data={"csrf_token": csrf_from(page),
                                                                     "allow": ["proxmox.reboot", "proxmox.snapshot"]})
    assert r.status_code == 303
    card = admin.get(f"/urzadzenia/{dev}").text
    assert url in card
    confirm = admin.get(url).text
    assert "Skutek:" in confirm and "docker" in confirm
    r = admin.post(url, data={"csrf_token": csrf_from(confirm)})
    assert r.status_code == 303 and r.headers["location"].startswith("/akcje/")
    view = admin.get(r.headers["location"]).text
    assert "wykonana" in view and "Uruchom ponownie" in view
    assert "Uruchom ponownie" in admin.get("/akcje").text


def test_niebezpieczna_po_wygasnieciu_potwierdzenia(app, pve, admin):
    app.state.adapter_transport = pve_actions()
    ref_id, dev = pve["docker"]
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.stop"])
    url = f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.stop"
    page = admin.get(url).text
    assert "/potwierdz?next=" in page and "<button class=\"btn danger\"" not in page
    r = admin.post(url, data={"csrf_token": csrf_from(page)})
    assert r.status_code == 403 and "hasłem" in r.text
    with app.state.engine.connect() as conn:
        assert not conn.execute(select(action_run)).first()


def test_podglad_nie_widzi_akcji(app, pve):
    make_user(app, "widz", "inne-dlugie-haslo-widza", role="podglad")
    with new_client(app) as c:
        do_login(c, "widz", "inne-dlugie-haslo-widza")
        ref_id, dev = pve["docker"]
        card = c.get(f"/urzadzenia/{dev}").text
        assert 'id="akcje"' not in card
        assert c.get(f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.snapshot").status_code == 403
        assert c.post(f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.snapshot",
                      data={"csrf_token": csrf_from(card)}).status_code == 403
        assert c.get("/akcje").status_code == 403


def test_wylacznik_w_panelu(app, pve, admin):
    page = admin.get("/akcje").text
    admin.post("/akcje/wylacz", data={"csrf_token": csrf_from(page)})
    with app.state.engine.connect() as conn:
        assert not actions.enabled(conn)
    # włączenie wymaga potwierdzenia
    assert admin.post("/akcje/wlacz", data={"csrf_token": csrf_from(page)}).headers["location"].startswith("/potwierdz")
    fresh_reauth(app, admin)
    admin.post("/akcje/wlacz", data={"csrf_token": csrf_from(page)})
    with app.state.engine.connect() as conn:
        assert actions.enabled(conn)


# ───────────────────────── agent ─────────────────────────
def agent_on_vm(app, pve, monkeypatch, local=("apt_upgrade",)):
    """Agent na VM docker (przypięty do VM 107 z Proxmoxa po MAC/IP) z lokalną listą akcji."""
    with app.state.engine.begin() as conn:
        code = agents.new_code(conn, "admin")
        iid, _token = agents.enroll(conn, code, "docker")
        report = fake_report(monkeypatch)
        report["actions"] = list(local)
        row = integrations.get(conn, iid)
        agents.record_report(conn, row, report)
        ref = conn.execute(select(external_ref).where(external_ref.c.integration_id == iid)).mappings().first()
        assert ref["device_id"] == pve["docker"][1]
    return iid, (ref["id"], ref["device_id"]), report


def test_apt_upgrade_przez_agenta_po_snapshocie(app, pve, monkeypatch):
    iid, target, report = agent_on_vm(app, pve, monkeypatch)
    with app.state.engine.begin() as conn:
        allow(conn, ["agent.apt_upgrade", "proxmox.snapshot"])
        first = req(conn, target, "agent.apt_upgrade", reauth_ok=True, snapshot_first=True)
        snap = actions.get(conn, first)
        upgrade = conn.execute(select(action_run).where(action_run.c.after_id == first)).mappings().first()
        assert snap["action"] == "proxmox.snapshot" and upgrade["status"] == "czeka"
        # dopóki snapshot się nie skończy, agent nie dostaje zlecenia
        assert actions.agent_exchange(conn, integrations.get(conn, iid), [])[0] == []
    run(actions.execute(app.state.engine, app.state.box, first, pve_actions(), wait=0.1))
    with app.state.engine.begin() as conn:
        r = actions.get(conn, upgrade["id"])
        assert r["status"] == "zlecona" and r["task"]["queued_at"]     # czas na odebranie liczy się od teraz
        row = integrations.get(conn, iid)
        jobs, _ = actions.agent_exchange(conn, row, [])
        assert jobs == [{"id": upgrade["id"], "action": "apt_upgrade"}]
        assert actions.agent_exchange(conn, row, [])[0] == []                 # zlecenie idzie raz
        actions.agent_exchange(conn, row, [{"id": upgrade["id"], "state": "trwa", "started": 1.0}])
        assert actions.get(conn, upgrade["id"])["status"] == "trwa"
        _, ack = actions.agent_exchange(conn, row, [{"id": upgrade["id"], "state": "ok", "rc": 0,
                                                     "output": "3 upgraded, 0 newly installed"}])
        assert ack == [upgrade["id"]]
        r = actions.get(conn, upgrade["id"])
        assert r["status"] == "ok" and "3 upgraded" in r["result"]


def test_nieudany_snapshot_wstrzymuje_aktualizacje(app, pve, monkeypatch):
    iid, target, _ = agent_on_vm(app, pve, monkeypatch)
    with app.state.engine.begin() as conn:
        allow(conn, ["agent.apt_upgrade", "proxmox.snapshot"])
        first = req(conn, target, "agent.apt_upgrade", reauth_ok=True, snapshot_first=True)
    run(actions.execute(app.state.engine, app.state.box, first, pve_actions(deny=True), wait=0))
    with app.state.engine.begin() as conn:
        upgrade = conn.execute(select(action_run).where(action_run.c.after_id == first)).mappings().first()
        assert upgrade["status"] == "blad" and "snapshot" in upgrade["result"].lower()
        assert actions.agent_exchange(conn, integrations.get(conn, iid), [])[0] == []


def test_agent_bez_lokalnej_zgody(app, pve, monkeypatch):
    iid, target, _ = agent_on_vm(app, pve, monkeypatch, local=())
    with app.state.engine.begin() as conn:
        allow(conn, ["agent.apt_upgrade"])
        [o] = [o for o in actions.for_device(conn, target[1]) if o["key"] == "agent.apt_upgrade"]
        assert "lokalna lista" in o["reason"]
        with pytest.raises(actions.ActionError, match="lokalna lista"):
            req(conn, target, "agent.apt_upgrade", reauth_ok=True)
        # obcy wynik (id przebiegu innej integracji) nie jest przyjmowany
        _, ack = actions.agent_exchange(conn, integrations.get(conn, iid), [{"id": 999, "state": "ok"}, "x", {"id": "1"}])
        assert ack == []


def test_aktualizacja_potem_restart_jesli_wymagany(app, pve, monkeypatch):
    iid, target, _ = agent_on_vm(app, pve, monkeypatch, local=("apt_upgrade", "reboot"))
    with app.state.engine.begin() as conn:
        allow(conn, ["agent.apt_upgrade"])
        with pytest.raises(actions.ActionError, match="Restart po aktualizacji"):
            req(conn, target, "agent.apt_upgrade", reauth_ok=True, reboot_after=True)
        allow(conn, ["agent.apt_upgrade", "agent.reboot"])
        upgrade = req(conn, target, "agent.apt_upgrade", reauth_ok=True, reboot_after=True)
        reboot = conn.execute(select(action_run).where(action_run.c.after_id == upgrade)).mappings().first()
        assert reboot["action"] == "agent.reboot" and reboot["status"] == "czeka"
        assert reboot["params"] == {"if_required": True}
        row = integrations.get(conn, iid)
        assert actions.agent_exchange(conn, row, [])[0] == [{"id": upgrade, "action": "apt_upgrade"}]
        # wynik aktualizacji i zlecenie restartu w tej samej odpowiedzi
        jobs, _ = actions.agent_exchange(conn, row, [{"id": upgrade, "state": "ok", "rc": 0, "output": "1 upgraded"}])
        assert jobs == [{"id": reboot["id"], "action": "reboot", "params": {"if_required": True}}]
        actions.agent_exchange(conn, row, [{"id": reboot["id"], "state": "ok", "rc": 0,
                                            "output": "Restart niepotrzebny — maszyna go nie wymaga."}])
        assert actions.get(conn, reboot["id"])["status"] == "ok"
        # nieudana aktualizacja = bez restartu
        upgrade = req(conn, target, "agent.apt_upgrade", reauth_ok=True, reboot_after=True)
        actions.agent_exchange(conn, row, [])
        actions.agent_exchange(conn, row, [{"id": upgrade, "state": "blad", "rc": 100, "output": "E: dpkg"}])
        reboot = conn.execute(select(action_run).where(action_run.c.after_id == upgrade)).mappings().first()
        assert reboot["status"] == "blad" and actions.agent_exchange(conn, row, [])[0] == []


def test_okno_aktualizacji_z_restartem(app, pve, monkeypatch, admin):
    _iid, (ref_id, dev), _ = agent_on_vm(app, pve, monkeypatch, local=("apt_upgrade", "reboot"))
    with app.state.engine.begin() as conn:
        allow(conn, ["agent.apt_upgrade"])
    url = f"/urzadzenia/{dev}/akcje/{ref_id}/agent.apt_upgrade"
    assert "Restart po aktualizacji: niedostępny" in admin.get(url).text
    with app.state.engine.begin() as conn:
        allow(conn, ["agent.apt_upgrade", "agent.reboot"])
    assert 'name="reboot" value="1"' in admin.get(url).text


def test_raport_agenta_przez_http_niesie_zlecenia(app, pve, monkeypatch, client):
    with app.state.engine.begin() as conn:
        code = agents.new_code(conn, "admin")
    token = client.post("/api/agent/v1/rejestracja", json={"code": code, "hostname": "docker"}).json()["token"]
    report = fake_report(monkeypatch)
    report["actions"] = ["apt_upgrade", "rm_rf"]
    assert client.post("/api/agent/v1/raport", json=report, headers={"Authorization": f"Bearer {token}"}).json() == \
        {"interval": 60, "jobs": [], "ack": []}
    with app.state.engine.begin() as conn:
        ref = conn.execute(select(external_ref).where(external_ref.c.external_id == "host")).mappings().first()
        st = integrations.states_for_device(conn, ref["device_id"])
        assert next(s for s in st if s["external_id"] == "host")["data"]["actions"] == ["apt_upgrade"]
        allow(conn, ["agent.apt_upgrade"])
        run_id = req(conn, (ref["id"], ref["device_id"]), "agent.apt_upgrade", reauth_ok=True)
    body = client.post("/api/agent/v1/raport", json=report, headers={"Authorization": f"Bearer {token}"}).json()
    assert body["jobs"] == [{"id": run_id, "action": "apt_upgrade"}]


# ───────────────────────── agent na maszynie ─────────────────────────
def test_agent_wykonuje_tylko_z_lokalnej_listy(tmp_path):
    jobs, results = tmp_path / "zlecenia", tmp_path / "wyniki"
    jobs.mkdir()
    results.mkdir()
    agent.write_jobs([{"id": 7, "action": "apt_upgrade"}, {"id": 8, "action": "rm_rf"}, {"id": "9"}, "x"],
                     str(jobs), str(results))
    assert sorted(p.name for p in jobs.iterdir()) == ["7.json"]          # nieznane akcje w ogóle nie trafiają na dysk
    (jobs / "8.json").write_text(json.dumps({"id": 8, "action": "apt_upgrade"}))
    (jobs / "smiec.txt").write_text("?")
    calls = []

    def fake_upgrade(params):
        calls.append(1)
        return True, 0, "1 upgraded"
    # lista pusta: nic się nie wykonuje, wynik mówi, jak pozwolić
    agent.do_jobs({"actions": []}, str(jobs), str(results), {"apt_upgrade": fake_upgrade})
    assert calls == [] and list(jobs.iterdir()) == []
    res = {r["id"]: r for r in agent.read_results(str(results))}
    assert res[7]["state"] == "blad" and "pozwol apt_upgrade" in res[7]["output"]
    agent.drop_results([7, 8], str(results))
    assert agent.read_results(str(results)) == []
    # na liście: wykonuje się raz
    agent.write_jobs([{"id": 10, "action": "apt_upgrade"}], str(jobs), str(results))
    agent.do_jobs({"actions": ["apt_upgrade"]}, str(jobs), str(results), {"apt_upgrade": fake_upgrade})
    agent.do_jobs({"actions": ["apt_upgrade"]}, str(jobs), str(results), {"apt_upgrade": fake_upgrade})
    assert calls == [1]
    [r] = agent.read_results(str(results))
    assert r["id"] == 10 and r["state"] == "ok" and r["output"] == "1 upgraded"


def test_agent_restart_maszyny(tmp_path, monkeypatch):
    jobs, results = tmp_path / "zlecenia", tmp_path / "wyniki"
    jobs.mkdir()
    results.mkdir()
    agent.write_jobs([{"id": 1, "action": "reboot", "params": {"if_required": True, "cmd": "rm -rf /"}},
                      {"id": 2, "action": "reboot", "params": {"if_required": "tak"}}], str(jobs), str(results))
    assert json.loads((jobs / "1.json").read_text()) == {"id": 1, "action": "reboot", "params": {"if_required": True}}
    assert json.loads((jobs / "2.json").read_text()) == {"id": 2, "action": "reboot"}
    calls = []
    monkeypatch.setattr(agent, "run", lambda cmd, **kw: calls.append(cmd) or (0, "", ""))
    monkeypatch.setattr(agent, "reboot", lambda: {"required": False, "packages": []})
    monkeypatch.setattr(agent, "kernel", lambda: {"running": "6.8.12-4", "newest": "6.8.12-4"})
    agent.do_jobs({"actions": ["reboot"]}, str(jobs), str(results))
    res = {r["id"]: r for r in agent.read_results(str(results))}
    assert res[1]["state"] == "ok" and "niepotrzebny" in res[1]["output"]       # tylko gdy wymagany — nie był
    assert res[2]["state"] == "ok" and "Restart za minutę" in res[2]["output"]  # zlecony wprost
    assert calls == [["shutdown", "-r", "+1", "Denali: restart zlecony z panelu"]]
    monkeypatch.setattr(agent, "kernel", lambda: {"running": "6.8.12-4", "newest": "6.8.12-5"})
    assert agent.reboot_required_now() and agent.reboot_machine({"if_required": True})[0]
    assert len(calls) == 2


def test_uslugi_akcji_agenta():
    main = agent.unit_text("denali-agent.service")
    assert f"ReadWritePaths=-{agent.JOBS_DIR} -{agent.RESULTS_DIR}" in main and "ProtectSystem=strict" in main
    assert f"DirectoryNotEmpty={agent.JOBS_DIR}" in agent.unit_text("denali-agent-akcje.path")
    svc = agent.unit_text("denali-agent-akcje.service")
    assert "User=" not in svc and f"{agent.PROGRAM} akcje" in svc
