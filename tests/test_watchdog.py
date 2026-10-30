"""Strażnik: meldunek co minutę do monitora na VPS, adres zaszyfrowany, błędy widoczne w panelu."""
import asyncio

import httpx
import pytest

from denali import watchdog
from denali.db import kv_get

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

KUMA = "https://kuma.example.pl/api/push/AbC123tajne?status=up&msg=OK&ping="


def run(coro):
    return asyncio.run(coro)


class FakeMonitor:
    def __init__(self, status=200):
        self.status, self.hits = status, []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.hits.append(request.url)
        return httpx.Response(self.status, json={"ok": True})


def test_meldunek_co_minute(conn, app):
    mon = FakeMonitor()
    watchdog.configure(conn, app.state.box, KUMA)
    assert "AbC123tajne" not in str(kv_get(conn, watchdog.KEY))                 # token zaszyfrowany
    assert run(watchdog.ping(conn, app.state.box, mon.transport, now=1000)) is True
    assert run(watchdog.ping(conn, app.state.box, mon.transport, now=1030)) is False     # jeszcze nie minęła minuta
    assert run(watchdog.ping(conn, app.state.box, mon.transport, now=1061)) is True
    assert len(mon.hits) == 2 and mon.hits[0].params["status"] == "up"
    assert watchdog.status(conn)["last_ok"] == 1061


def test_blad_monitora_widoczny(conn, app):
    watchdog.configure(conn, app.state.box, KUMA)
    assert run(watchdog.ping(conn, app.state.box, FakeMonitor(404).transport, now=1000)) is False
    assert "błąd 404" in watchdog.status(conn)["last_error"]


def test_wylaczony_niczego_nie_wysyla(conn, app):
    mon = FakeMonitor()
    assert run(watchdog.ping(conn, app.state.box, mon.transport, now=1000)) is False
    watchdog.configure(conn, app.state.box, KUMA)
    watchdog.configure(conn, app.state.box, "")
    assert run(watchdog.ping(conn, app.state.box, mon.transport, now=2000)) is False and mon.hits == []


def test_tylko_https(conn, app):
    with pytest.raises(watchdog.WatchdogError):
        watchdog.configure(conn, app.state.box, "http://kuma.lan/api/push/x")


def test_ustawienie_w_panelu(app):
    mon = FakeMonitor()
    app.state.watchdog_transport = mon.transport
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        page = c.get("/admin/powiadomienia").text
        assert "Strażnik na VPS" in page and "Wyłączony" in page
        r = c.post("/admin/powiadomienia/straznik", data={"csrf_token": csrf_from(page), "url": KUMA})
        assert "pierwszy meldunek doszedł" in r.text and "AbC123tajne" not in r.text
    assert len(mon.hits) == 1
