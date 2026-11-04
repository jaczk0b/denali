"""Historia stanów, planowane przerwy, migotanie, sprawdzenia TCP/DNS, odczyt integracji w tle i /metrics."""
import asyncio
import socket

import pytest
from sqlalchemy import select

from denali import checks, history, incidents, integrations, inventory as inv, live, maintenance, profiles
from denali.models import incident, state_change
from denali.web import metrics_routes

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client


@pytest.fixture
def admin_client(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


def run(coro):
    return asyncio.run(coro)


def ping(conn, device_id, ok, now):
    spec = checks.Spec(device_id, "ping", "192.168.1.10")
    checks.record(conn, [(spec, checks.Result(ok, "ping 1 ms" if ok else "192.168.1.10 nie odpowiada", 1.0 if ok else None))],
                  [spec], now)


def lab(conn):
    pve = inv.create_device(conn, "pve", "host")
    vm = inv.create_device(conn, "docker-vm", "vm", parent_id=pve)
    inv.add_address(conn, pve, "ip", "192.168.1.10")
    return pve, vm, profiles.value(conn, pve, "down_after_s")


def open_auto(conn):
    return conn.execute(select(incident).where(incident.c.status != "zamkniety")).mappings().all()


# ───────────────────────── oczekiwanie przed awarią ─────────────────────────
def test_niedzialajacy_ping_mowi_kiedy_bedzie_awaria(conn):
    pve, _, down_after = lab(conn)
    ping(conn, pve, False, 1000)
    state = live.compute(conn, now=1010)[pve]
    assert state["state"] == "unknown" and state["pending_until"] == 1000 + down_after
    assert "uznam to za awarię" in state["why"]
    assert "pending_until" not in live.compute(conn, now=1000 + down_after)[pve]


# ───────────────────────── historia ─────────────────────────
def test_zmiany_stanu_i_pasek_dostepnosci(conn):
    pve, _, down_after = lab(conn)
    ping(conn, pve, True, 1000)
    live.evaluate(conn, now=1000)
    live.evaluate(conn, now=1060)                       # bez zmiany — bez nowego wpisu
    ping(conn, pve, False, 2000)
    live.evaluate(conn, now=2000 + down_after)
    ping(conn, pve, True, 3000)
    live.evaluate(conn, now=3000)
    rows = conn.execute(select(state_change.c.state).where(state_change.c.device_id == pve)
                        .order_by(state_change.c.ts)).scalars().all()
    assert rows == ["ok", "down", "ok"]
    a = history.availability(conn, pve, 1, now=1000 + 3600)
    assert a["down_s"] == pytest.approx(3000 - 2000 - down_after)
    assert "down" in a["slots"] and a["slots"][-1] == "ok"
    assert 0 < a["pct"] < 100


def test_probki_czasu_odpowiedzi_co_5_minut(conn):
    pve, _, _ = lab(conn)
    assert history.record_samples(conn, [(pve, "ping", True, 3.0)], now=1000) == 1
    assert history.record_samples(conn, [(pve, "ping", True, 4.0)], now=1100) == 0
    assert history.record_samples(conn, [(pve, "ping", False, None)], now=1400) == 1
    spark = history.sparkline(history.response_times(conn, pve, "ping", 1, now=1500), 1500 - 3600, 1500)
    assert spark["fails"] and spark["avg"] == 3.0 and spark["d"].startswith("M")
    history.prune(conn, now=1400 + history.KEEP_SAMPLES_DAYS * 86400 + 1)
    assert not history.response_times(conn, pve, "ping", 24 * 400, now=1500 + 400 * 86400)


# ───────────────────────── planowana przerwa ─────────────────────────
def test_przerwa_nie_otwiera_incydentu_ani_dla_zaleznych(conn):
    pve, vm, down_after = lab(conn)
    maintenance.start(conn, pve, 30, "aktualizacja Proxmoxa", "admin", now=1000)
    ping(conn, pve, False, 1000)
    assert live.evaluate(conn, now=1000 + down_after) == []
    assert open_auto(conn) == []
    state = live.saved(conn, now=1000 + down_after)[pve]
    assert state["state"] == "off" and "Planowana przerwa do" in state["why"] and "aktualizacja" in state["why"]
    covered = maintenance.covered(conn, inv.load_graph(conn), now=1100)
    assert set(covered) == {pve, vm}
    # po przerwie awaria znowu się liczy
    assert live.evaluate(conn, now=1000 + 31 * 60)[0].cause == pve


def test_przerwa_walidacja_i_koniec(conn):
    pve, _, _ = lab(conn)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.start(conn, pve, 1, now=1000)
    mid = maintenance.start(conn, pve, 60, now=1000)
    maintenance.end(conn, mid, pve, now=1200)
    assert maintenance.active(conn, now=1300) == {}


def test_przerwa_z_karty_urzadzenia(app, admin_client):
    with app.state.engine.begin() as conn:
        pve, _, _ = lab(conn)
    page = admin_client.get(f"/urzadzenia/{pve}").text
    assert "Zacznij przerwę" in page
    r = admin_client.post(f"/urzadzenia/{pve}/przerwa", data={"csrf_token": csrf_from(page), "minutes": "30",
                                                              "reason": "restart"})
    assert r.status_code == 303
    page = admin_client.get(f"/urzadzenia/{pve}").text
    assert "Planowana przerwa do" in page and "Zakończ teraz" in page


# ───────────────────────── migotanie ─────────────────────────
def test_migotanie_trzyma_jeden_incydent(conn):
    pve, _, down_after = lab(conn)
    t = 1000
    ids = set()
    for _ in range(live.FLAP_COUNT):
        ping(conn, pve, False, t)
        live.evaluate(conn, now=t + down_after)
        ids |= {i["id"] for i in open_auto(conn)}
        ping(conn, pve, True, t + down_after + 30)
        live.evaluate(conn, now=t + down_after + 30)
        t += down_after + 60
    [inc] = open_auto(conn)                             # trzecia przerwa: nie zamyka się od razu
    detail = incidents.get_incident(conn, inc["id"])
    assert any(e["kind"] == "niestabilne" for e in detail["events"])
    back = t - 30
    live.evaluate(conn, now=back + live.FLAP_CALM + 1)
    assert open_auto(conn) == []
    closed = conn.execute(select(incident).where(incident.c.id == inc["id"])).mappings().first()
    assert closed["ended_at"] == pytest.approx(back)  # koniec = chwila powrotu, nie zamknięcia


# ───────────────────────── TCP i DNS ─────────────────────────
def test_porty_tcp_walidacja_i_plan(conn):
    pve, _, _ = lab(conn)
    with pytest.raises(inv.InventoryError):
        inv.update_device(conn, pve, tcp_ports="80, 70000")
    inv.update_device(conn, pve, tcp_ports="5432 1883,5432")
    assert inv.get_device(conn, pve)["tcp_ports"] == "1883, 5432"
    inv.add_address(conn, pve, "hostname", "pve.lan")
    specs = {(s.kind, s.target, s.expect) for s in checks.plan(conn) if s.device_id == pve}
    assert ("tcp", "192.168.1.10:1883", "") in specs and ("tcp", "192.168.1.10:5432", "") in specs
    assert ("dns", "pve.lan", "192.168.1.10") in specs


def test_sprawdzenie_tcp_na_prawdziwym_gniezdzie():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    try:
        assert run(checks.tcp(f"127.0.0.1:{port}")).ok
    finally:
        srv.close()
    res = run(checks.tcp(f"127.0.0.1:{port}", timeout=1))
    assert not res.ok and str(port) in res.summary


def test_dns_zly_adres_to_tylko_uwaga(conn):
    pve, _, down_after = lab(conn)
    assert run(checks.dns("localhost", "127.0.0.1")).ok
    bad = run(checks.dns("localhost", "10.9.9.9"))
    assert not bad.ok and "10.9.9.9" in bad.summary
    spec = checks.Spec(pve, "dns", "pve.lan", "192.168.1.10")
    checks.record(conn, [(spec, bad)], [spec], 1000)
    assert live.compute(conn, now=1000 + down_after)[pve]["state"] == "warn"


# ───────────────────────── odczyt integracji w tle ─────────────────────────
def test_wolna_integracja_nie_wstrzymuje_workera(app, monkeypatch):
    started = []

    async def slow(engine, box, row, transport=None, now=None):
        started.append(row["id"])
        await asyncio.sleep(10)

    monkeypatch.setattr(integrations, "poll_one", slow)
    monkeypatch.setattr(integrations, "due", lambda conn, now=None: [{"id": 1}, {"id": 2}])

    async def scenario():
        running = {}
        assert integrations.start_due(app.state.engine, app.state.box, running) == 2
        await asyncio.sleep(0)
        assert integrations.start_due(app.state.engine, app.state.box, running) == 0   # trwa — nie drugi raz
        for t in running.values():
            t.cancel()
        await asyncio.gather(*running.values(), return_exceptions=True)
        return running

    run(asyncio.wait_for(scenario(), 2))
    assert sorted(started) == [1, 2]


# ───────────────────────── /metrics i /api/stan ─────────────────────────
def test_metryki_wylaczone_bez_tokenu(app):
    with new_client(app) as c:
        assert c.get("/metrics").status_code == 404
        assert c.get("/api/stan").status_code == 404


def test_metryki_z_tokenem(app):
    app.state.settings.metrics_token = "sekretny-token-metryk"
    with app.state.engine.begin() as conn:
        pve, _, _ = lab(conn)
        ping(conn, pve, True, 1000)
        live.evaluate(conn)
    with new_client(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer zly"}).status_code == 401
        r = c.get("/metrics", headers={"Authorization": "Bearer sekretny-token-metryk"})
        assert r.status_code == 200
        assert 'denali_device_up{device="pve",kind="host"} 1' in r.text
        assert 'denali_check_response_ms{device="pve",kind="ping",target="192.168.1.10"} 1.0' in r.text
        j = c.get("/api/stan", headers={"Authorization": "Bearer sekretny-token-metryk"}).json()
    assert {d["name"]: d["state"] for d in j["devices"]}["pve"] == "ok"
    assert j["incidents"] == [] and j["worker_age_s"] is None


def test_etykiety_prometheus_sa_escapowane():
    snap = {"devices": [{"id": 1, "name": 'zły "host"\n', "kind": "vm", "state": "down", "maintenance_until": None}],
            "checks": [], "incidents": [], "updates_pending": 0, "worker_age_s": None}
    text = metrics_routes.prometheus(snap)
    assert 'device="zły \\"host\\" "' in text
