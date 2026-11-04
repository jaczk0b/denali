import asyncio
import time

import pytest
from sqlalchemy import inspect, text

from denali import __version__
from denali.config import Settings
from denali.db import kv_get, kv_set, make_engine
from denali.worker import run as worker_run


def test_migracje_tworza_schemat_w_trybie_wal(settings):
    engine = make_engine(settings.db_url)
    with engine.connect() as conn:
        assert "setting" in inspect(conn).get_table_names()
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    engine.dispose()


def test_migracje_mozna_uruchomic_ponownie(settings):
    from denali.migrate import upgrade
    upgrade(settings)  # drugi raz bez błędu — tak startuje kontener


def test_bez_kluczy_aplikacja_nie_wstaje(tmp_path):
    with pytest.raises(SystemExit, match="DENALI_ENCRYPTION_KEY"):
        Settings(_env_file=None, data_dir=tmp_path).check()


def test_tryb_deweloperski_dziala_bez_kluczy(tmp_path):
    Settings(_env_file=None, data_dir=tmp_path, dev=True).check()


def test_health_bez_workera(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "uwaga", "version": __version__, "db": "ok", "worker": "brak"}


def test_health_z_workerem_i_opoznionym_workerem(client, app):
    with app.state.engine.begin() as conn:
        kv_set(conn, "worker.heartbeat", time.time())
    assert client.get("/health").json()["worker"] == "ok"
    with app.state.engine.begin() as conn:
        kv_set(conn, "worker.heartbeat", time.time() - 600)
    body = client.get("/health").json()
    assert body["worker"] == "opóźniony" and body["status"] == "uwaga"


def test_naglowki_bezpieczenstwa(client):
    h = client.get("/health").headers
    assert h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"


def test_opis_api_ukryty_na_produkcji(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_worker_zapisuje_sygnal_zycia(settings):
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker_run(settings, stop, interval=0.05))
        await asyncio.sleep(0.2)
        stop.set()
        await task

    before = time.time()
    asyncio.run(scenario())
    engine = make_engine(settings.db_url)
    with engine.connect() as conn:
        assert kv_get(conn, "worker.heartbeat") >= before
    engine.dispose()


def test_haslo_zamiast_klucza_jest_odrzucone(tmp_path):
    with pytest.raises(SystemExit, match="nie jest klucz szyfrowania"):
        Settings(_env_file=None, data_dir=tmp_path, encryption_key="Haslo123").check()


def test_migracja_0013_usuwa_adresy_hassio(settings):
    """Adresy z sieci Home Assistanta dopisane przez integrację znikają; wpisane ręcznie i z LAN zostają."""
    from alembic import command

    from denali.migrate import alembic_config
    cfg = alembic_config(settings)
    command.downgrade(cfg, "0012")
    engine = make_engine(settings.db_url)
    with engine.begin() as conn:
        # surowy SQL: model zna już kolumny z późniejszych migracji
        did = conn.execute(text("INSERT INTO device (name, kind, role, criticality, place, serial, notes_md, icon, "
                                "app_url, pulpit_group, created_at, updated_at) VALUES "
                                "('ha', 'vm', '', 3, '', '', '', '', '', 'brak', 0, 0)")).lastrowid
        for value, source in [("172.30.32.1", "integracja"), ("172.30.33.5", "integracja"),
                              ("192.168.1.40", "integracja"), ("172.30.32.9", "manual")]:
            conn.execute(text("INSERT INTO device_address (device_id, kind, value, source, is_primary) "
                              "VALUES (:d, 'ip', :v, :s, 0)"), {"d": did, "v": value, "s": source})
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        left = {r.value for r in conn.execute(text("SELECT value FROM device_address WHERE device_id = :d"), {"d": did})}
    engine.dispose()
    assert left == {"192.168.1.40", "172.30.32.9"}
