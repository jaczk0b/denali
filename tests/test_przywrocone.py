"""Funkcje przywrócone z HLC: alerty, brak internetu, oś czasu, terminy, własne sprawdzenia, kanały, eksport, import."""
import asyncio
import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from denali import (agents, alerts, changes, checks, export_md, integrations, inventory as inv, live, maintenance, notify,
                    oui, profiles, terms, uplink, weekly)
from denali.adapters import PollResult, State
from denali.models import alert, change_event, custom_check, external_ref, incident, notification_channel

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

WEBHOOK = "https://discord.com/api/webhooks/123/abc-tajne"
T0 = 1_800_000_000.0

spec = importlib.util.spec_from_file_location("denali_agent", Path(__file__).parent.parent / "denali" / "agent" / "denali-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def run(coro):
    return asyncio.run(coro)


class FakeChannel:
    def __init__(self, status=200):
        self.status, self.sent = status, []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.sent.append((str(request.url), request.headers, json.loads(request.content)))
        return httpx.Response(self.status)


def source(conn, name="vm", alarms=(), state="warn", external_id="host", itype="proxmox", now=T0):
    """Urządzenie z zatwierdzonym obiektem integracji, który zgłasza podane alarmy."""
    did = inv.create_device(conn, name, "vm")
    iid = conn.execute(integrations.integration.insert().values(type=itype, name=f"i-{name}", config={}, poll_seconds=60)
                       ).inserted_primary_key[0]
    conn.execute(external_ref.insert().values(integration_id=iid, external_id=external_id, device_id=did, status="zatwierdzony",
                                              proposed={}, first_seen=now, last_seen=now))
    integrations.record(conn, iid, PollResult(states=[State(external_id, state, "x", {"alarms": list(alarms)})]), now)
    return did, iid


def report(conn, iid, alarms, state="warn", now=T0, external_id="host"):
    integrations.record(conn, iid, PollResult(states=[State(external_id, state, "x", {"alarms": list(alarms)})]), now)


POOL = {"rule": "pool_unhealthy", "key": "tank", "text": "Pula tank: DEGRADED"}


# ───────────────────────── alerty ─────────────────────────
def test_uwaga_bez_awarii_tworzy_alert_i_powiadomienie(conn, app):
    did, _ = source(conn, alarms=[POOL])
    notify.save_channel(conn, app.state.box, "D", WEBHOOK)
    assert alerts.evaluate(conn, T0) == 1
    fake = FakeChannel(204)
    assert run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + 10)) == 0   # czeka
    assert run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + alerts.ALERT_WAIT + 1)) == 1
    assert "Pula tank: DEGRADED" in fake.sent[0][2]["embeds"][0]["description"]
    assert run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + 700)) == 0     # raz
    assert conn.execute(select(func.count()).select_from(incident)).scalar() == 0


def test_rozwiazany_po_ciszy_i_jedno_powiadomienie(conn, app):
    _, iid = source(conn, alarms=[POOL])
    notify.save_channel(conn, app.state.box, "D", WEBHOOK)
    fake = FakeChannel(204)
    alerts.evaluate(conn, T0)
    run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + 400))
    report(conn, iid, [], "ok", T0 + 500)
    alerts.evaluate(conn, T0 + 500)
    assert alerts.active(conn, now=T0 + 500)                  # chwilowy powrót nie zamyka alertu
    alerts.evaluate(conn, T0 + 1200)
    assert not alerts.active(conn, now=T0 + 1200)
    assert run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + 1201)) == 1
    assert "Rozwiązany" in fake.sent[-1][2]["embeds"][0]["title"]


def test_milczace_zrodlo_nie_rozwiazuje_alertu(conn, app):
    _, iid = source(conn, alarms=[POOL])
    alerts.evaluate(conn, T0)
    conn.execute(integrations.observed_state.update().where(integrations.observed_state.c.integration_id == iid)
                 .values(state="unknown"))
    alerts.evaluate(conn, T0 + 5000)
    assert len(alerts.active(conn, now=T0 + 5000)) == 1


def test_progi_dysku_z_profilu(conn):
    item = {"rule": "disk", "key": "/", "pct": 90, "text": "dysk"}
    did, _ = source(conn, alarms=[item])
    alerts.evaluate(conn, T0)
    assert alerts.active(conn, now=T0)[0]["severity"] == "ostrzezenie"
    profiles.set_override(conn, did, "disk_warn", 95)
    alerts.evaluate(conn, T0 + 1000)
    alerts.evaluate(conn, T0 + 2000)
    assert not alerts.active(conn, now=T0 + 2000)
    with pytest.raises(profiles.ProfileError):
        profiles.set_override(conn, did, "disk_warn", 500)


def test_wyciszenie_przerwa_i_profil(conn, app):
    did, _ = source(conn, alarms=[POOL])
    notify.save_channel(conn, app.state.box, "D", WEBHOOK)
    alerts.evaluate(conn, T0)
    aid = alerts.active(conn, now=T0)[0]["id"]
    now = T0 + 400
    alerts.silence(conn, aid, 7, now)
    assert alerts.to_notify(conn, {}, now) == ([], [])
    alerts.unsilence(conn, aid)
    assert alerts.to_notify(conn, {did: {"state": "down"}}, now) == ([], [])       # mówi o tym incydent
    profiles.set_override(conn, did, "notify_warn", False)
    send, skip = alerts.to_notify(conn, {}, now)
    assert not send and len(skip) == 1


def test_certyfikat_i_termin_daja_alert(conn):
    did = inv.create_device(conn, "www", "usluga")
    spec_ = checks.Spec(did, "http", "https://www.example.com/")
    checks.record(conn, [(spec_, checks.Result(True, "ok", cert_expires_at=T0 + 3 * 86400))], [spec_], T0)
    terms.add(conn, did, time.strftime("%Y-%m-%d", time.localtime(T0 + 10 * 86400)), "gwarancja")
    alerts.evaluate(conn, T0)
    rows = {a["rule"]: a for a in alerts.active(conn, now=T0)}
    assert rows["cert"]["severity"] == "krytyczny"
    assert "gwarancja" in rows["term"]["message"] and rows["term"]["severity"] == "ostrzezenie"


# ───────────────────────── źródła ─────────────────────────
def test_agent_zglasza_dysk_core_i_usluge():
    rep = {"version": "1.4.0", "hostname": "vm", "virt": "kvm", "addresses": [{"kind": "ip", "value": "10.0.0.5"}],
           "disks": [{"mount": "/", "pct": 96, "free_gb": 3}], "memory": {"memory": 99},
           "failed_units": ["x.service"], "boot_id": "a",
           "containers": [{"name": "immich", "image": "i", "image_id": "s", "state": "running", "size_rw": 3 * 1024 ** 3,
                           "core_files": 23, "health": "unhealthy"}]}
    st = agents.to_result(rep, T0).states[0]
    rules = {a["rule"] for a in st.data["alarms"]}
    assert {"disk", "memory", "systemd", "container_layer", "core_dumps", "container_unhealthy"} <= rules
    assert st.state == "warn" and st.data["ips"] == ["10.0.0.5"]


def test_starszy_agent_bez_nowych_pol():
    st = agents.to_result({"version": "1.3.0", "hostname": "vm", "virt": "kvm"}, T0).states[0]
    assert st.state == "ok" and "containers" not in st.data and agents.outdated("1.3.0") and not agents.outdated(agents.latest_version())


def test_agent_parsery_i_zlecenia_nie_biora_sciezek_z_panelu(tmp_path):
    assert agent.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 25 kB\nSwapTotal: 0 kB\n") == {"memory": 75.0}
    assert agent.find_core_files([{"Path": "/app/core.123", "Kind": 1}, {"Path": "/app/core.py", "Kind": 1},
                                  {"Path": "/a/node_modules/core", "Kind": 1}]) == ["/app/core.123"]
    calls = []

    def api(path, method="GET", timeout=120):
        return [{"Id": "abc", "Names": ["/immich"]}] if path.startswith("/containers/json") else \
            [{"Path": "/core.1", "Kind": 1}]

    def runner(cmd, timeout=0):
        calls.append(cmd)
        return 0, "/core.1\n", ""
    ok, _, out = agent.clean_cores({"paths": ["/etc/passwd"]}, api=api, runner=runner)
    assert ok and "/etc/passwd" not in calls[0] and "/core.1" in calls[0] and "immich: 1" in out


def test_agent_stary_wynik_dockera_jest_ignorowany(tmp_path):
    p = tmp_path / "docker.json"
    p.write_text(json.dumps({"ts": T0, "containers": [{"name": "a"}]}))
    assert agent.load_docker(str(p), now=T0 + 60) == [{"name": "a"}]
    assert agent.load_docker(str(p), now=T0 + agent.DOCKER_MAX_AGE + 1) is None
    assert {"denali-agent-docker.service", "denali-agent-docker.timer"} <= set(agent.UNITS)
    assert "PrivateNetwork=yes" in agent.unit_text("denali-agent-docker.service")


def test_akcje_agenta_zgodne_z_panelem():
    assert set(agents.AGENT_ACTIONS) == set(agent.ACTIONS) == set(agent.RUNNERS)
    assert {a.key for a in agents.Agent.actions} == set(agents.AGENT_ACTIONS)


# ───────────────────────── brak internetu ─────────────────────────
def test_brak_internetu_nie_jest_awaria_vps(conn):
    vps = inv.create_device(conn, "vps", "vps")
    lan = inv.create_device(conn, "nas", "nas")
    a, b = checks.Spec(vps, "ping", "51.68.1.1"), checks.Spec(lan, "ping", "192.168.1.5")
    checks.record(conn, [(a, checks.Result(False, "nie odpowiada")), (b, checks.Result(False, "nie odpowiada"))], [a, b], T0)
    uplink.record(conn, False, T0)
    assert not uplink.is_down(conn)
    uplink.record(conn, False, T0 + 30)
    assert uplink.is_down(conn)
    states = live.compute(conn, T0 + 500)
    assert states[vps]["state"] == "unknown" and states[lan]["state"] == "down"
    alerts.evaluate(conn, T0 + 500)
    assert [x["rule"] for x in alerts.active(conn, now=T0 + 500)] == ["internet_down"]
    uplink.record(conn, True, T0 + 600)
    assert live.compute(conn, T0 + 700)[vps]["state"] == "down"       # z internetem to już prawdziwa awaria


def test_internet_o_przerwie_mowi_po_powrocie(conn, app):
    notify.save_channel(conn, app.state.box, "D", WEBHOOK)
    uplink.record(conn, False, T0)
    uplink.record(conn, False, T0 + 30)
    alerts.evaluate(conn, T0 + 30)
    fake = FakeChannel(204)
    assert run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + 900)) == 0
    uplink.record(conn, True, T0 + 1000)
    alerts.evaluate(conn, T0 + 1000)
    alerts.evaluate(conn, T0 + 1700)
    assert run(notify.process_alerts(conn, app.state.box, "https://d", {}, fake.transport, T0 + 1701)) == 1
    assert "Brak internetu" in fake.sent[0][2]["embeds"][0]["description"]


def test_uplink_online():
    async def tak(host):
        return host == "9.9.9.9"

    async def nie(host):
        return False
    assert run(uplink.online(tak)) and not run(uplink.online(nie))


def test_needs_internet():
    assert checks.needs_internet("ping", "51.68.1.1") and checks.needs_internet("http", "https://immich.example.com/")
    assert not checks.needs_internet("ping", "192.168.1.5") and not checks.needs_internet("http", "http://nas.lan/")
    assert not checks.needs_internet("tcp", "10.0.0.1:5432")


# ───────────────────────── zmiany ─────────────────────────
def test_zmiany_spoza_panelu():
    old = {"boot_id": "a", "uptime": 90000, "status": "running", "ips": ["10.0.0.1"], "dpkg_changed": 1,
           "containers": [{"name": "a", "image_id": "1", "restarts": 0}, {"name": "b", "image_id": "2"}]}
    new = {"boot_id": "b", "uptime": 60, "status": "stopped", "ips": ["10.0.0.2"], "dpkg_changed": 2,
           "containers": [{"name": "a", "image_id": "9", "restarts": 2, "image": "x:2"}, {"name": "c", "image": "y"}]}
    kinds = [k for k, _ in changes.diff(old, new)]
    assert kinds.count("restart") == 3 and {"stan", "adres", "pakiety", "kontener", "obraz"} <= set(kinds)
    assert changes.diff({}, new) == []
    assert changes.diff({"containers": None}, {"containers": []}) == []


def test_zmiana_trafia_do_incydentu_z_24_godzin(conn):
    did, iid = source(conn, state="ok")
    integrations.record(conn, iid, PollResult(states=[State("host", "ok", "x", {"boot_id": "a"})]), T0)
    integrations.record(conn, iid, PollResult(states=[State("host", "ok", "x", {"boot_id": "b"})]), T0 + 100)
    assert conn.execute(select(change_event.c.kind)).scalars().all() == ["restart"]
    g = inv.load_graph(conn)
    assert any("restart systemu" in text for _, text in changes.before(conn, g, did, T0 + 20 * 3600))
    assert not changes.before(conn, g, did, T0 + 30 * 3600)
    assert changes.timeline(conn, T0)[0]["device"] == "vm"


# ───────────────────────── sprawdzenia i terminy ─────────────────────────
def test_wlasne_sprawdzenia(conn):
    did = inv.create_device(conn, "www", "usluga")
    checks.add_custom(conn, did, "http", "https://inna.example.com/zdrowie")
    checks.add_custom(conn, did, "tls", "10.0.0.1:8443")
    with pytest.raises(checks.CheckError):
        checks.add_custom(conn, did, "http", "https://inna.example.com/zdrowie")
    for kind, target in (("http", "ftp://x"), ("tcp", "bez-portu"), ("dns", "nas.lan"), ("exec", "x")):
        with pytest.raises(checks.CheckError):
            checks.add_custom(conn, did, kind, target)
    plan = {(s.kind, s.target) for s in checks.plan(conn)}
    assert ("tls", "10.0.0.1:8443") in plan and ("http", "https://inna.example.com/zdrowie") in plan


def test_tls_uzywa_daty_certyfikatu(monkeypatch):
    monkeypatch.setattr(checks, "cert_expiry", lambda host, port: T0 + 86400 * 40)
    res = run(checks.tls("10.0.0.1:8443", now=T0))
    assert res.ok and res.cert_expires_at == T0 + 86400 * 40
    monkeypatch.setattr(checks, "cert_expiry", lambda host, port: T0 - 10)
    assert not run(checks.tls("10.0.0.1:8443", now=T0)).ok


def test_terminy(conn):
    did = inv.create_device(conn, "ups", "inne")
    with pytest.raises(terms.TermError):
        terms.add(conn, did, "jutro", "x")
    with pytest.raises(terms.TermError):
        terms.add(conn, did, "2030-01-01", " ")
    tid = terms.add(conn, did, "2030-01-01", "gwarancja")
    assert terms.due(conn, datetime(2030, 1, 1).timestamp() - 86400)[0]["id"] == tid
    assert terms.due(conn, datetime(2020, 1, 1).timestamp()) == []
    terms.remove(conn, did + 1, tid)
    assert len(terms.for_device(conn, did)) == 1
    terms.remove(conn, did, tid)
    assert not terms.for_device(conn, did)


# ───────────────────────── kanały ─────────────────────────
def test_ntfy_telegram_email(conn, app, monkeypatch):
    box = app.state.box
    fake = FakeChannel()
    a = notify.save_other(conn, box, "ntfy", "n", {"url": "https://ntfy.example.com/", "topic": "dom", "token": "t"})
    b = notify.save_other(conn, box, "telegram", "t", {"token": "123456:" + "a" * 25, "chat_id": "42"})
    c = notify.save_other(conn, box, "email", "e", {"host": "smtp.example.com", "sender": "a@b.c", "to": "d@e.f"})
    mails = []
    monkeypatch.setattr(notify, "_smtp_send", lambda cfg, subject, body: mails.append((cfg["host"], subject, body)))
    for cid in (a, b, c):
        run(notify.test_channel(conn, box, cid, "https://d", fake.transport))
    assert fake.sent[0][0].rstrip("/") == "https://ntfy.example.com" and fake.sent[0][2]["topic"] == "dom"
    assert fake.sent[0][1]["authorization"] == "Bearer t"
    assert fake.sent[1][2]["chat_id"] == "42" and "api.telegram.org" in fake.sent[1][0]
    assert mails[0][1] == "[Denali] Test powiadomień Denali"
    row = conn.execute(select(notification_channel).where(notification_channel.c.id == b)).mappings().first()
    assert "aaaa" not in row["config_ct"]


def test_zle_ustawienia_kanalu(conn, app):
    for type_, form in (("ntfy", {"url": "ftp://x", "topic": "a"}), ("ntfy", {"url": "https://x", "topic": "a b"}),
                        ("telegram", {"token": "zly", "chat_id": "1"}), ("email", {"host": "h"}), ("sms", {})):
        with pytest.raises(notify.NotifyError):
            notify.save_other(conn, app.state.box, type_, "x", form)


def test_powtorka_krytycznego_incydentu(conn, app):
    notify.save_channel(conn, app.state.box, "D", WEBHOOK)
    did = inv.create_device(conn, "nas", "nas", criticality=1)
    iid = conn.execute(incident.insert().values(title="nas nie działa", root_device_id=did, severity="krytyczny", source="auto",
                                                status="trwa", started_at=T0, notified_at=T0)).inserted_primary_key[0]
    fake = FakeChannel(204)
    assert run(notify.reminders(conn, app.state.box, "https://d", fake.transport, T0 + 90000)) == 0    # domyślnie wyłączone
    notify.set_repeat_hours(conn, 4)
    assert run(notify.reminders(conn, app.state.box, "https://d", fake.transport, T0 + 3600)) == 0
    assert run(notify.reminders(conn, app.state.box, "https://d", fake.transport, T0 + 4 * 3600)) == 1
    assert fake.sent[0][2]["embeds"][0]["title"].startswith("Nadal:")
    assert run(notify.reminders(conn, app.state.box, "https://d", fake.transport, T0 + 4 * 3600 + 60)) == 0
    conn.execute(incident.update().where(incident.c.id == iid).values(status="potwierdzony"))
    assert run(notify.reminders(conn, app.state.box, "https://d", fake.transport, T0 + 90000)) == 0


def test_raport_miesieczny(conn, app):
    notify.save_channel(conn, app.state.box, "D", WEBHOOK)
    inv.create_device(conn, "nas", "nas")
    fake = FakeChannel(204)
    assert run(weekly.process_month(conn, app.state.box, "https://d", fake.transport, force=True))
    assert fake.sent[0][2]["embeds"][0]["title"].startswith("Dostępność")


# ───────────────────────── eksport i producent MAC ─────────────────────────
def test_eksport_maskuje_sekrety(conn):
    did = inv.create_device(conn, "nas", "nas", notes_md="root hasło: Tajne123!\nDB_PASSWORD=sekret-z-env")
    text = export_md.for_device(conn, did)
    assert "Tajne123!" not in text and "sekret-z-env" not in text and "# nas" in text
    assert "nas" in export_md.overview(conn)


def test_producent_mac(tmp_path):
    (tmp_path / "oui.txt").write_text("286FB9     (base 16)\t\tNokia Shanghai Bell Co., Ltd.\n", encoding="utf-8")
    assert oui.lookup(tmp_path, "28:6f:b9:00:11:22") == "Nokia Shanghai Bell Co., Ltd."
    assert oui.lookup(tmp_path, "aa:bb:cc:00:11:22") == ""
    assert oui.lookup(tmp_path / "brak", "28:6f:b9:00:11:22") == ""


def test_lista_producentow_pobierana_raz(tmp_path):
    body = "286FB9     (base 16)\t\tNokia\n"
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(200, text=body)
    state = {}
    transport = httpx.MockTransport(handle)
    assert run(oui.refresh(tmp_path, state, transport, now=time.time()))
    assert not run(oui.refresh(tmp_path, state, transport, now=time.time()))
    assert len(calls) == 1


# ───────────────────────── interfejs ─────────────────────────
def admin(app, client):
    make_user(app)
    do_login(client)
    enable_2fa(client, app)
    return client


def test_strony_i_formularze(app, client):
    admin(app, client)
    with app.state.engine.begin() as c:
        did, _ = source(c, alarms=[POOL])
        alerts.evaluate(c, time.time())
        inv.add_address(c, did, "ip", "10.0.0.7")
        aid = c.execute(select(alert.c.id)).scalar()
    page = client.get("/alerty")
    assert page.status_code == 200 and "Pula tank" in page.text
    csrf = csrf_from(page.text)
    assert client.post(f"/alerty/{aid}/wycisz", data={"csrf_token": csrf, "days": 7}).status_code == 303
    assert "wyciszony do" in client.get("/alerty").text
    assert client.post(f"/alerty/{aid}/odcisz", data={"csrf_token": csrf}).status_code == 303
    assert client.post(f"/urzadzenia/{did}/terminy", data={"csrf_token": csrf, "due_on": "2030-05-01", "label": "domena"}).status_code == 303
    assert client.post(f"/urzadzenia/{did}/terminy", data={"csrf_token": csrf, "due_on": "zle", "label": "x"}).status_code == 400
    assert client.post(f"/urzadzenia/{did}/sprawdzenia", data={"csrf_token": csrf, "kind": "tcp", "target": "10.0.0.7:5432"}).status_code == 303
    card = client.get(f"/urzadzenia/{did}").text
    assert "domena" in card and "10.0.0.7:5432" in card and "Wymaga uwagi" in card
    with app.state.engine.connect() as c:
        cid = c.execute(select(custom_check.c.id)).scalar()
    assert client.post(f"/urzadzenia/{did}/sprawdzenia/{cid}/usun", data={"csrf_token": csrf}).status_code == 303
    assert client.get("/os-czasu").status_code == 200
    assert client.get("/os-czasu?dni=30&urzadzenie=%d" % did).status_code == 200
    assert client.get("/eksport.md").headers["content-type"].startswith("text/markdown")
    assert client.get(f"/urzadzenia/{did}/eksport.md").status_code == 200
    assert f'href="/urzadzenia/{did}"' in client.get("/urzadzenia?q=10.0.0").text
    assert "Nic nie pasuje" in client.get("/urzadzenia?q=nie-ma-takiego").text
    assert "Wymaga uwagi" in client.get("/").text
    assert "Inny kanał" in client.get("/admin/powiadomienia").text
    assert client.post("/admin/powiadomienia/powtorki", data={"csrf_token": csrf, "hours": 4}).status_code == 303
    assert client.post("/admin/powiadomienia/powtorki", data={"csrf_token": csrf, "hours": 5}).status_code == 400
    assert client.post("/admin/powiadomienia/inny", data={"csrf_token": csrf, "type": "sms"}).status_code == 400


def test_podglad_nie_zmienia_ani_nie_eksportuje(app):
    make_user(app)
    make_user(app, "oko", role="podglad")
    with app.state.engine.begin() as c:
        did = inv.create_device(c, "nas", "nas")
    with new_client(app) as cl:
        do_login(cl, "oko")
        assert cl.get("/alerty").status_code == 200
        assert cl.get("/eksport.md").status_code in (303, 403)
        assert cl.get(f"/urzadzenia/{did}/eksport.md").status_code in (303, 403)
        assert cl.post(f"/urzadzenia/{did}/terminy", data={"due_on": "2030-01-01", "label": "x"}).status_code in (303, 400, 403)
    with app.state.engine.connect() as c:
        assert not terms.for_device(c, did)


def test_przerwa_blokuje_powiadomienie_o_alercie(conn, app):
    did, _ = source(conn, alarms=[POOL])
    alerts.evaluate(conn, T0)
    now = time.time()
    conn.execute(alert.update().values(first_seen=now - 1000))
    maintenance.start(conn, did, 30, "test", "admin", now)
    assert alerts.to_notify(conn, {}, now) == ([], [])


# ───────────────────────── adaptery i uprawnienia ─────────────────────────
def alarms_of(result, external_id):
    return next(s for s in result.states if s.external_id == external_id).data["alarms"]


def test_proxmox_magazyny_dysk_lxc_i_wezel():
    from denali.adapters import registry

    from .test_proxmox import CONFIG, RESOURCES, SECRETS, pve_api
    res = [dict(r) for r in RESOURCES]
    for r in res:
        if r.get("type") == "lxc":
            r.update(status="running", disk=9, maxdisk=10)
        if r.get("type") == "node":
            r.update(disk=96, maxdisk=100, mem=95, maxmem=100, status="online")
    res.append({"type": "storage", "storage": "local-lvm", "node": "pve", "disk": 96, "maxdisk": 100, "status": "available"})
    res.append({"type": "storage", "storage": "nieaktywny", "node": "pve", "disk": 99, "maxdisk": 100, "status": "unavailable"})
    out = run(registry()["proxmox"](CONFIG, SECRETS, transport=pve_api(resources=res)).poll())
    node = alarms_of(out, "node/pve")
    assert {a["rule"] for a in node} == {"disk", "memory"} and node[0]["pct"] == 96.0
    assert alarms_of(out, "lxc/110")[0]["pct"] == 90.0
    assert alarms_of(out, "qemu/100") == []                     # dysk VM zna tylko agent
    store = next(s for s in out.states if s.external_id == "storage/pve/local-lvm")
    assert store.state == "warn" and store.data["alarms"][0]["severity"] == "krytyczny"
    assert store.data["usage"]["local-lvm"]["total"] == 100     # forecast dostaje magazyn Proxmoxa
    assert not any("nieaktywny" in s.external_id for s in out.states)


def test_truenas_pula_smart_i_zadania(monkeypatch):
    import copy

    from denali.adapters import registry

    from . import test_pbs_truenas as tn
    data = copy.deepcopy(tn.TN_DATA)
    data["pool.query"][0].update(status="DEGRADED", healthy=False, size=100, allocated=96)
    data["alert.list"] = [{"level": "CRITICAL", "formatted": "Dysk padł", "klass": "Disk"},
                          {"level": "INFO", "formatted": "Nowa wersja", "klass": "Update"}]
    data["replication.query"] = [{"name": "off", "state": {"state": "ERROR", "error": "brak miejsca"}}]
    monkeypatch.setattr(tn, "TN_DATA", data)
    out = run(registry()["truenas"](tn.TN_CONFIG, tn.TN_SECRETS, transport=tn.truenas_rest()).poll())
    pool = next(s for s in out.states if s.external_id.startswith("pool/"))
    assert {a["rule"] for a in pool.data["alarms"]} == {"pool_unhealthy", "storage"}
    server = alarms_of(out, "server")
    assert [(a["rule"], a["severity"]) for a in server] == [("truenas_alert", "krytyczny")]
    assert alarms_of(out, "replication/off")[0]["rule"] == "backup_task"


def test_truenas_smart_i_temperatura():
    from denali.adapters.truenas import _disk_states
    disks = _disk_states({"disk.query": [{"name": "sda", "serial": "S1", "type": "HDD"}], "disk.temperatures": {"sda": 60}})
    assert [a["rule"] for a in disks[0].data["alarms"]] == ["disk_hot"]


def test_pbs_stara_kopia():
    from denali.adapters import registry

    from .test_pbs_truenas import PBS_CONFIG, PBS_SECRETS, pbs_api
    out = run(registry()["pbs"](PBS_CONFIG, PBS_SECRETS, transport=pbs_api()).poll())
    stale = [s for s in out.states if s.state == "warn" and s.external_id.startswith("backup/")]
    assert stale and stale[0].data["alarms"][0]["rule"] == "backup_stale"


def test_komodo_stack_niezdrowy(monkeypatch):
    import copy

    from denali.adapters import registry

    from . import test_komodo as k
    stacks = copy.deepcopy(k.STACKS)
    stacks[0]["info"]["state"] = "unhealthy"
    monkeypatch.setattr(k, "STACKS", stacks)
    out = run(registry()["komodo"](k.CONFIG, k.SECRETS, transport=k.komodo_api()).poll())
    assert [a["rule"] for s in out.states for a in s.data.get("alarms", [])] == ["stack_unhealthy"] * 2    # st1 i nagrany st2


def test_uzytkownik_nie_widzi_alertow(app):
    make_user(app)
    make_user(app, "gosc", role="uzytkownik")
    with new_client(app) as cl:
        do_login(cl, "gosc")
        for path in ("/alerty", "/os-czasu"):
            assert cl.get(path).status_code in (303, 403)


def test_import_terminy_porty_sprawdzenia_sieci_zdarzenia(app, settings, tmp_path):
    import sqlite3

    from denali import hlc_import
    from denali.models import device, device_term, file as file_table, runbook

    from .test_hlc_import import OLD_KEY, make_hlc
    make_hlc(tmp_path)
    db = sqlite3.connect(tmp_path / "instance" / "hlc.db")
    now = time.time()
    db.execute("UPDATE devices SET ports=?, expiry=? WHERE id='immich'", (
        json.dumps([{"port": 2283, "proto": "tcp"}, {"port": 53, "proto": "udp"}]),
        json.dumps([{"date": "2031-02-03", "label": "gwarancja"}, {"date": "kiedyś", "label": "zła"}])))
    db.execute("UPDATE sites SET subnets=? WHERE id='s1'", (json.dumps([{"cidr": "192.168.1.0/24", "name": "LAN", "dhcpFrom": 100, "dhcpTo": 199}]),))
    db.executescript("DROP TABLE checks; CREATE TABLE checks (id TEXT, device_id TEXT, name TEXT, type TEXT, target TEXT, "
                     "params TEXT DEFAULT '{}', scope TEXT DEFAULT 'lan', enabled INTEGER DEFAULT 1);"
                     "CREATE TABLE config_versions (id INTEGER, device_id TEXT, path TEXT, sha TEXT, content TEXT, ts REAL);"
                     "CREATE TABLE events (id INTEGER, ts REAL, device_id TEXT, kind TEXT, title TEXT);")
    db.execute("INSERT INTO checks VALUES ('c1','immich','web','http','https://immich.lan/api','{}','lan',1)")
    db.execute("INSERT INTO checks VALUES ('c2','immich','zew','http','https://immich.example.com','{}','external',1)")
    db.execute("INSERT INTO config_versions VALUES (1,'immich','/opt/immich/.env','a','STARA=1',1)")
    db.execute("INSERT INTO config_versions VALUES (2,'immich','/opt/immich/.env','b','NOWA=2',2)")
    db.execute("INSERT INTO events VALUES (1,?,'immich','image_changed','immich: nowy obraz')", (now - 3600,))
    db.execute("INSERT INTO events VALUES (2,?,'immich','image_changed','za stare')", (now - 200 * 86400,))
    db.commit()
    db.close()

    def go():
        return hlc_import.migrate(app.state.engine, settings, app.state.box, tmp_path / "instance", OLD_KEY, ["Dom"], apply=True)
    report = go()
    assert report.counts["terminy"] == 1 and report.counts["własne sprawdzenia"] == 1
    assert any("zła" in w for w in report.warnings)
    with app.state.engine.connect() as c:
        row = c.execute(select(device).where(device.c.name == "Immich")).mappings().first()
        assert row["tcp_ports"] == "2283"
        assert c.execute(select(device_term.c.label)).scalars().all() == ["gwarancja"]
        assert c.execute(select(custom_check.c.target)).scalars().all() == ["https://immich.lan/api"]
        assert c.execute(select(change_event.c.text)).scalars().all() == ["immich: nowy obraz"]
        assert c.execute(select(func.count()).select_from(runbook).where(runbook.c.title.like("Sieci%"))).scalar() == 1
        names = c.execute(select(file_table.c.name)).scalars().all()
    assert sum(n.startswith("konfiguracja-") for n in names) == 1
    go()            # drugi przebieg niczego nie dubluje
    with app.state.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(device_term)).scalar() == 1
        assert c.execute(select(func.count()).select_from(change_event)).scalar() == 1
        assert c.execute(select(func.count()).select_from(custom_check)).scalar() == 1


def test_zmiana_stanu_puli_nie_mowi_o_proxmoxie():
    assert changes.diff({"status": "ONLINE"}, {"status": "DEGRADED"}) == [("stan", "ONLINE → DEGRADED")]


def test_db_backup_pomija_klientow_bazy(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_BACKUP_DIR", str(tmp_path))

    def api(path, method="GET", timeout=120):
        if path == "/containers/json":
            return [{"Id": "a", "Names": ["/aplikacja"]}]
        return {"Config": {"Image": "aplikacja:1", "Env": ["POSTGRES_HOST=db", "POSTGRES_PASSWORD=x"]}}
    assert agent.db_backup(api=api) == (True, 0, "Nie znaleziono kontenerów Postgres.")


def test_migracja_zachowuje_incydenty(tmp_path):
    from alembic import command
    from alembic.config import Config
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, text

    from denali import migrate
    from denali.config import Settings
    settings = Settings(_env_file=None, data_dir=tmp_path, encryption_key=Fernet.generate_key().decode())
    cfg = migrate.alembic_config(settings) if hasattr(migrate, "alembic_config") else None
    if cfg is None:
        pytest.skip("brak alembic_config")
    command.upgrade(cfg, "0020")
    engine = create_engine(settings.db_url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO incident (id, title, severity, source, status, started_at, cause_md, fix_md, acked_by) "
                       "VALUES (1, 't', 'krytyczny', 'auto', 'trwa', 1, '', '', '')"))
        c.execute(text("INSERT INTO incident_event (incident_id, ts, kind, text, user) VALUES (1, 1, 'otwarty', 'x', '')"))
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM incident_event")).scalar() == 1
        assert c.execute(text("SELECT reminded_at FROM incident")).scalar() is None
    engine.dispose()


def test_obieg_workera(settings):
    """Jeden prawdziwy obieg z alertem i kanałem: czy wszystko jest podpięte, bez wyjątków w logu."""
    import logging

    from denali import worker
    from denali.crypto import Box
    from denali.db import make_engine

    engine = make_engine(settings.db_url)
    box = Box(settings.encryption_key)
    with engine.begin() as c:
        source(c, alarms=[POOL], now=time.time())
        c.execute(notification_channel.insert().values(type="discord", name="x", config_ct=box.encrypt(json.dumps({"webhook": WEBHOOK})),
                                                       min_severity="ostrzezenie", enabled=False))
    errors = []

    class Catch(logging.Handler):
        def emit(self, record):
            errors.append(record.getMessage())

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(settings, stop, interval=0.1))
        await asyncio.sleep(6)
        stop.set()
        await task

    handler = Catch(level=logging.ERROR)
    logging.getLogger("denali.worker").addHandler(handler)
    try:
        run(go())
    finally:
        logging.getLogger("denali.worker").removeHandler(handler)
    assert errors == []
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(alert)).scalar() == 1
    engine.dispose()


def test_odmowa_websocketu_ma_konkretny_powod(app, client):
    from starlette.websockets import WebSocketDisconnect

    from denali import consoles, ssh
    admin(app, client)
    with app.state.engine.begin() as c:
        did = inv.create_device(c, "vm", "vm")
        inv.add_address(c, did, "ip", "10.0.0.9")
        cid = consoles.request_ssh(c, device_id=did, host="10.0.0.9", port=22, login="jan", user="admin", role="admin",
                                   reauth_ok=True)
    app.state.console_tickets[cid] = {"adapter": None, "info": None, "at": time.time()}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/terminale/konsola/{cid}/ws", headers={"origin": "https://obcy.example\x1b[31m"}):
            pass
    reason = client.get(f"/terminale/konsola/{cid}/koniec").json()["reason"]
    assert "obcy.example" in reason and "DENALI_BASE_URL" in reason and "Websockets Support" not in reason
    assert "\x1b" not in reason


def test_bez_websocketu_zostaje_wskazowka_o_proxy(app, client):
    from denali import consoles
    admin(app, client)
    with app.state.engine.begin() as c:
        did = inv.create_device(c, "vm", "vm")
        inv.add_address(c, did, "ip", "10.0.0.9")
        cid = consoles.request_ssh(c, device_id=did, host="10.0.0.9", port=22, login="jan", user="admin", role="admin",
                                   reauth_ok=True)
    app.state.console_tickets[cid] = {"adapter": None, "info": None, "at": time.time()}
    assert "Websockets Support" in client.get(f"/terminale/konsola/{cid}/koniec").json()["reason"]


def test_dzial_powiadomienia(app, client):
    from denali.models import incident as inc_table
    admin(app, client)
    page = client.get("/incydenty").text
    top = page[page.index('class="nav nav-main"'):page.index("</nav>")]
    assert 'href="/powiadomienia"' in top and "/alerty" not in top and "/aktualizacje" not in top
    for path, tab in (("/incydenty", "Incydenty"), ("/alerty", "Alerty"), ("/aktualizacje", "Aktualizacje"), ("/os-czasu", "Oś czasu")):
        html = client.get(path).text
        assert f'href="{path}" aria-current="page">{tab}' in html
    assert client.get("/powiadomienia").headers["location"] == "/incydenty"
    with app.state.engine.begin() as c:
        source(c, alarms=[POOL], now=time.time())
        alerts.evaluate(c, time.time())
    assert client.get("/powiadomienia").headers["location"] == "/alerty"
    with app.state.engine.begin() as c:
        c.execute(inc_table.insert().values(title="x", severity="krytyczny", source="auto", status="trwa", started_at=time.time(),
                                            acked_by="", cause_md="", fix_md=""))
    assert client.get("/powiadomienia").headers["location"] == "/incydenty"
    assert "Kanały powiadomień" in client.get("/").text


def test_wyciszony_alert_nie_liczy_sie_w_menu(app, client):
    admin(app, client)
    with app.state.engine.begin() as c:
        source(c, alarms=[POOL], now=time.time())
        alerts.evaluate(c, time.time())
        alerts.silence(c, c.execute(select(alert.c.id)).scalar(), 7)
    assert '<span class="badge">' not in client.get("/alerty").text
    assert client.get("/powiadomienia").headers["location"] == "/incydenty"
