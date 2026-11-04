"""Werdykt pulpitu, motyw, Ctrl+K, „co jeśli”, zmiany przed awarią, restart z przerwą, plan restartu hosta,
„Nadchodzące”, prognoza miejsca i podsumowanie tygodnia."""
import asyncio
import time

from sqlalchemy import select

from denali import (changes, checks, forecast, incidents, inventory as inv, live, maintenance, mapview, status,
                    upcoming, weekly)
from denali.adapters import State
from denali.db import kv_set
from denali.models import action_run, config_backup, incident, integration, usage_sample

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client
from .test_akcje import allow, pve, req  # noqa: F401 — fikstura pve
from .test_notify import WEBHOOK, FakeDiscord


def run(coro):
    return asyncio.run(coro)


def lab(conn):
    pve_ = inv.create_device(conn, "pve", "host", criticality=1)
    nas = inv.create_device(conn, "truenas", "vm", parent_id=pve_)
    docker = inv.create_device(conn, "docker-vm", "vm", parent_id=pve_)
    immich = inv.create_device(conn, "immich", "kontener", parent_id=docker)
    inv.add_dependency(conn, docker, nas, "dane")
    return pve_, nas, docker, immich


def viewer(app):
    make_user(app, "domownik", "haslo-domownika-123", "podglad")
    c = new_client(app)
    do_login(c, "domownik", "haslo-domownika-123")
    return c


# ───────────────────────── pulpit ─────────────────────────
def test_werdykt():
    devs = [{"id": 1, "name": "pve"}, {"id": 2, "name": "nas"}]
    assert status.verdict(devs, {}, [], {})["state"] == "unknown"
    v = status.verdict(devs, {1: {"state": "ok", "why": ""}, 2: {"state": "ok", "why": ""}}, [], {},
                       {"ended_at": 1000}, now=1000 + 3 * 86400)
    assert v["headline"] == "Wszystko działa" and "bez awarii od 3 dni" in v["sub"]
    v = status.verdict(devs, {1: {"state": "warn", "why": ""}, 2: {"state": "ok", "why": ""}}, [], {})
    assert v["state"] == "warn" and v["sub"] == "pve"
    inc = [{"id": 7, "title": "nas nie działa", "severity": "krytyczny", "status": "trwa", "started_at": 1000,
            "root_device_id": 2}]
    v = status.verdict(devs, {2: {"state": "down", "why": "ping nie odpowiada"}}, inc, {1: {"ends_at": 5000}})
    assert v["state"] == "down" and v["headline"] == "1 problem: nas nie działa" and v["sub"] == "ping nie odpowiada"
    assert v["link"] == "/incydenty/7" and v["pauses"] and v["pauses"][0].startswith("pve do ")


def test_pulpit_pokazuje_werdykt_i_podpowiedzi(app):
    with app.state.engine.begin() as conn:
        d = inv.create_device(conn, "Immich", "usluga", app_url="https://immich.lan")
    c = viewer(app)
    page = c.get("/").text
    assert "Nie wiem, co działa" in page and "Ikony aplikacji" in page
    assert "+ Immich" not in page                       # przypinać może tylko administrator
    assert d


# ───────────────────────── motyw i Ctrl+K ─────────────────────────
def test_motyw_w_ciasteczku(app):
    c = viewer(app)
    page = c.get("/procedury").text
    assert 'data-theme="grafit"' in page
    r = c.post("/motyw", data={"csrf_token": csrf_from(page), "motyw": "zorza", "next": "/procedury"})
    assert r.status_code == 303 and r.headers["location"] == "/procedury"
    assert 'data-theme="zorza"' in c.get("/procedury").text
    c.post("/motyw", data={"csrf_token": csrf_from(page), "motyw": "<script>", "next": "//zly.pl"})
    assert 'data-theme="grafit"' in c.get("/procedury").text


def test_szybkie_przejscie(app):
    with app.state.engine.begin() as conn:
        lab(conn)
    c = viewer(app)
    pages = c.get("/szukaj/szybko").json()
    assert {"label": "Mapa", "sub": "strona", "url": "/infrastruktura/mapa"} in pages
    assert not any(p["url"] == "/admin/audyt" for p in pages)          # strony administratora tylko dla niego
    found = c.get("/szukaj/szybko?q=docker").json()
    assert found[0]["label"] == "docker-vm" and found[-1]["url"] == "/szukaj?q=docker"


# ───────────────────────── mapa ─────────────────────────
def test_co_jesli_i_lancuch(app, conn):
    pve_, nas, docker, immich = lab(conn)
    marks, sim = mapview.simulation(conn, nas)
    assert sim["name"] == "truenas" and sim["hard"] == ["docker-vm", "immich"]
    m = mapview.build(conn, {}, marks, simulate=nas)
    states = {n["id"]: n["state"] for n in m["nodes"]}
    assert states[nas] == "down" and states[docker] == "effect" and states[immich] == "effect"
    rel = {n["id"]: set(n["rel"].split()) for n in m["nodes"]}
    assert {str(nas), str(pve_)} <= rel[docker] and str(docker) in rel[immich]
    assert mapview.simulation(conn, 9999) == (None, None)


def test_strona_co_jesli(app):
    with app.state.engine.begin() as conn:
        _, nas, _, _ = lab(conn)
    c = viewer(app)
    page = c.get(f"/infrastruktura/mapa?co_jesli={nas}").text
    assert "Gdyby padło truenas" in page and "docker-vm, immich" in page
    assert "co_jesli=" in c.get("/infrastruktura/mapa?co_jesli=0").text


# ───────────────────────── zmiany przed awarią ─────────────────────────
def test_zmiany_przed_awaria_trafiaja_do_incydentu(conn):
    pve_, nas, docker, _ = lab(conn)
    t = time.time() + 600                       # „teraz” awarii: 10 minut po założeniu urządzeń
    conn.execute(action_run.insert().values(created_at=t - 100, device_id=pve_, external_id="node/pve",
                                            action="proxmox.reboot", label="Uruchom ponownie", status="ok",
                                            requested_by="jakub", finished_at=t - 50))
    for ts, sha in ((t - 9000, "a" * 64), (t - 20, "b" * 64)):
        conn.execute(config_backup.insert().values(device_id=pve_, source="x", name="k", taken_at=ts, checked_at=ts,
                                                   size=1, sha256=sha, data_ct="x"))
    inv.update_device(conn, nas, now=t - 10, role="dane")
    g = inv.load_graph(conn)
    texts = [t_ for _, t_ in changes.before(conn, g, docker, now=t)]
    assert any("Uruchom ponownie na pve (jakub) — wykonana" in t for t in texts)
    assert any("konfiguracja pve jest inna" in t for t in texts)
    assert any("zmieniono dokumentację truenas" in t for t in texts)
    assert texts[0].startswith("chwilę wcześniej") or "min wcześniej" in texts[0]
    # przez live.evaluate: incydent dostaje wpisy „zmiana”
    spec = checks.Spec(docker, "ping", "10.0.0.5")
    checks.record(conn, [(spec, checks.Result(False, "10.0.0.5 nie odpowiada"))], [spec], t - 1000)
    live.evaluate(conn, now=t)
    [inc] = conn.execute(select(incident)).mappings().all()
    kinds = [e["kind"] for e in incidents.get_incident(conn, inc["id"])["events"]]
    assert "objaw" in kinds and "zmiana" in kinds


# ───────────────────────── restart z przerwą i plan restartu hosta ─────────────────────────
def test_restart_z_panelu_zaklada_przerwe(app, pve):  # noqa: F811
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.reboot", "proxmox.snapshot"])
        req(conn, pve["docker"], "proxmox.reboot", reauth_ok=True, now=1000)
        [m] = maintenance.active(conn, now=1100).values()
        assert m["device_id"] == pve["docker"][1] and m["ends_at"] == 1000 + 15 * 60 and "z panelu" in m["reason"]
        req(conn, pve["test"], "proxmox.snapshot", now=1200)           # snapshot nie przerywa pracy — bez przerwy
        assert len(maintenance.active(conn, now=1200)) == 1


def test_plan_restartu_hosta(app, conn):
    pve_, nas, docker, immich = lab(conn)
    plan = maintenance.host_plan(conn, inv.load_graph(conn), pve_)
    assert [g["name"] for g in plan["shutdown"]] == ["docker-vm", "truenas"]   # docker czyta z NAS — wyłącz najpierw
    assert [g["name"] for g in plan["start"]] == ["truenas", "docker-vm"]
    assert plan["shutdown"][0]["needs"] == ["truenas"] and plan["shutdown"][0]["apps"] == ["immich"]


# ───────────────────────── nadchodzące i prognoza ─────────────────────────
def test_nadchodzace(conn):
    pve_, _, _, _ = lab(conn)
    spec = checks.Spec(pve_, "http", "https://pve.lan:8006")
    res = checks.Result(True, "HTTP 200", 3.0, cert_expires_at=1000 + 5 * 86400)
    checks.record(conn, [(spec, res)], [spec], 1000)
    kv_set(conn, "backup.last_ok", 1000 - 3 * 86400)
    items = upcoming.items(conn, now=1000)
    assert items[0]["text"] == "Ostatnia kopia Denali 3 dni temu — sprawdź workera"
    assert items[1]["text"] == "Certyfikat pve.lan:8006 wygasa za 5 dni" and items[1]["level"] == "down"


def test_prognoza_zapelnienia(conn):
    iid = conn.execute(integration.insert().values(type="truenas", name="nas", config={}, poll_seconds=60)
                       ).inserted_primary_key[0]
    tb = 1e12
    for day in range(1, 6):                                # przybywa 10 GB dziennie, pula 1 TB, zajęte 50%
        st = State("pool/tank", "ok", "", {"size": tb, "allocated": tb / 2 + (day - 1) * 1e10})
        assert forecast.record(conn, iid, [st], now=day * 86400) == 1
    assert forecast.record(conn, iid, [st], now=5 * 86400 + 60) == 0        # najwyżej raz na godzinę
    [f] = forecast.forecasts(conn, now=5 * 86400 + 1)
    assert f["name"] == "pula tank" and round(f["per_day"] / 1e9) == 10
    assert round(f["days"]) == round((0.95 * tb - (tb / 2 + 4e10)) / 1e10)
    assert forecast.alerts_due(conn, now=5 * 86400) == []                     # 41 dni — jeszcze bez alarmu
    assert "pula tank" in upcoming.items(conn, now=5 * 86400)[0]["text"]
    assert len(conn.execute(select(usage_sample)).all()) == 5


def test_prognoza_alarm_raz_na_tydzien(app, conn):
    from denali import notify
    discord = FakeDiscord()
    notify.save_channel(conn, app.state.box, "Discord", WEBHOOK)
    iid = conn.execute(integration.insert().values(type="truenas", name="nas", config={}, poll_seconds=60)
                       ).inserted_primary_key[0]
    for day in range(1, 4):
        forecast.record(conn, iid, [State("pool/tank", "ok", "", {"size": 100.0, "allocated": 80.0 + day * 2})],
                        now=day * 86400)
    assert run(notify.forecast_alerts(conn, app.state.box, "https://d", discord.transport, now=3 * 86400)) == 1
    assert "Kończy się miejsce" == discord.sent[0]["embeds"][0]["title"]
    assert run(notify.forecast_alerts(conn, app.state.box, "https://d", discord.transport, now=4 * 86400)) == 0


# ───────────────────────── podsumowanie tygodnia ─────────────────────────
def test_podsumowanie_tygodnia(app, conn):
    from datetime import datetime
    from denali import notify
    discord = FakeDiscord()
    notify.save_channel(conn, app.state.box, "Discord", WEBHOOK, "krytyczny")     # próg nie dotyczy raportu
    lab(conn)
    monday = datetime(2026, 11, 2, 9, 0).timestamp()
    assert weekly.due(conn, monday)[0] and not weekly.due(conn, monday - 3 * 3600)[0]
    assert run(weekly.process(conn, app.state.box, "https://d", discord.transport, now=monday))
    text = discord.sent[0]["embeds"][0]["description"]
    assert "**Incydenty:** żadnych" in text and "**Dostępność:**" in text
    assert not run(weekly.process(conn, app.state.box, "https://d", discord.transport, now=monday + 3600))


def test_strona_restartu_hosta_i_przypinanie(app):
    make_user(app)
    with app.state.engine.begin() as conn:
        pve_, nas, docker, _ = lab(conn)
        app_id = inv.create_device(conn, "Grafana", "usluga", app_url="http://grafana.lan")
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        page = c.get(f"/urzadzenia/{pve_}/restart-hosta").text
        assert "Bezpieczny restart pve" in page
        assert page.index("docker-vm") < page.index("truenas")              # najpierw wyłącz docker-vm
        assert c.get(f"/urzadzenia/{docker}/restart-hosta").status_code == 404    # to nie host
        desk = c.get("/").text
        assert "+ Grafana" in desk
        r = c.post(f"/urzadzenia/{app_id}/na-pulpit", data={"csrf_token": csrf_from(desk)})
        assert r.status_code == 303
        assert "+ Grafana" not in c.get("/").text
