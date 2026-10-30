"""Odświeżanie na żywo (SSE): odcisk stanu, strumień, które strony słuchają, i że nic nie przedłuża sesji."""
import re
import time

import pytest
from sqlalchemy import select, update

from denali import inventory as inv, live
from denali.db import kv_set
from denali.models import session
from denali.web import live_routes

from .conftest import do_login, enable_2fa, make_user, new_client


@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


@pytest.fixture
def fast_stream(monkeypatch):
    """Strumień kończy się po ułamku sekundy — TestClient zwraca odpowiedź dopiero po jej końcu."""
    monkeypatch.setattr(live_routes, "POLL_S", 0.01)
    monkeypatch.setattr(live_routes, "MAX_S", 0.05)


def events(text):
    return re.findall(r"event: (\w+)\ndata: ?(.*)\n\n", text)


def last_seen(app):
    with app.state.engine.connect() as c:
        return c.execute(select(session.c.last_seen_at).where(session.c.kind == "zwykla")).scalar()


def age_sessions(app, seconds):
    with app.state.engine.begin() as c:
        c.execute(update(session).values(last_seen_at=time.time() - seconds))


def test_odcisk_zmienia_sie_tylko_przy_zmianie_stanu(conn):
    now = time.time()
    v0 = live.version(conn, now)
    assert live.version(conn, now) == v0
    d = inv.create_device(conn, "nas", "nas")
    v1 = live.version(conn, now)
    assert v1 != v0
    kv_set(conn, live.STATES_KEY, {"at": now, "devices": {str(d): {"state": "ok", "why": "ping"}}})
    v2 = live.version(conn, now)
    assert v2 != v1
    kv_set(conn, live.STATES_KEY, {"at": now, "devices": {str(d): {"state": "down", "why": "cisza"}}})
    assert live.version(conn, now) != v2
    # worker przestał pisać: stan jest nieaktualny, więc strona też musi się zmienić („brak danych”)
    assert live.version(conn, now + live.STALE_AFTER + 1) != live.version(conn, now)


def test_strumien_wysyla_odcisk_strony(app, admin, fast_stream):
    page = admin.get("/").text
    v = re.search(r'data-live="([0-9a-f]+)"', page).group(1)
    r = admin.get("/na-zywo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    assert events(r.text) == [("stan", v)]


def test_strumien_bez_logowania(client):
    r = client.get("/na-zywo")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_strumien_konczy_sie_po_wylogowaniu(app, admin, fast_stream, monkeypatch):
    real = live_routes._peek
    calls = []

    def peek_then_revoke(engine, token, ip):
        calls.append(1)
        if len(calls) == 2:
            with engine.begin() as c:
                c.execute(session.delete())
        return real(engine, token, ip)

    monkeypatch.setattr(live_routes, "_peek", peek_then_revoke)
    monkeypatch.setattr(live_routes, "MAX_S", 5)
    r = admin.get("/na-zywo")
    assert [e for e, _ in events(r.text)] == ["stan", "koniec"]


def test_odswiezanie_nie_przedluza_sesji(app, admin, fast_stream):
    age_sessions(app, 3600)
    before = last_seen(app)
    admin.get("/na-zywo")
    admin.get("/", headers={"X-Denali-Live": "1"})
    assert last_seen(app) == before
    admin.get("/")    # zwykłe wejście to aktywność
    assert last_seen(app) > before


def test_ktore_strony_sluchaja(app, admin):
    with app.state.engine.begin() as c:
        d = inv.create_device(c, "nas", "nas")
    for path in ("/", "/urzadzenia", "/incydenty"):
        assert "data-live=" in admin.get(path).text, path
    # karta urządzenia ma skarbiec i formularze — podmiana schowałaby odsłonięte hasło
    assert "data-live=" not in admin.get(f"/urzadzenia/{d}").text
    assert "data-live=" not in admin.get("/procedury").text
    assert '<script src="/static/denali.js" defer></script>' in admin.get("/procedury").text
    assert admin.get("/static/denali.js").status_code == 200
