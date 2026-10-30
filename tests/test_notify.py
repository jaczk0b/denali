"""Powiadomienia na Discord: jedno na awarię, po zebraniu skutków, „rozwiązane”, wyciszenie, błędy wysyłki."""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from denali import incidents, inventory as inv, notify, profiles
from denali.models import incident, notification_channel

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

WEBHOOK = "https://discord.com/api/webhooks/123/abc-tajne"


def run(coro):
    return asyncio.run(coro)


class FakeDiscord:
    def __init__(self, status=204):
        self.status, self.sent = status, []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.sent.append(json.loads(request.content))
        return httpx.Response(self.status)


def lab(conn, app, severity_p1=True, min_severity="ostrzezenie"):
    notify.save_channel(conn, app.state.box, "Discord", WEBHOOK, min_severity)
    pve = inv.create_device(conn, "pve", "host")
    truenas = inv.create_device(conn, "truenas", "vm", parent_id=pve, criticality=1 if severity_p1 else 3)
    immich = inv.create_device(conn, "Immich", "kontener", parent_id=truenas)
    g = inv.load_graph(conn)
    [f] = g.analyze({pve: "ok", truenas: "down", immich: "down"})
    iid, _ = incidents.record_finding(conn, g, f, now=1000)
    return iid, truenas


def test_jedno_powiadomienie_po_zebraniu_skutkow_i_rozwiazane(conn, app):
    discord = FakeDiscord()
    iid, truenas = lab(conn, app)
    assert run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1030)) == 0
    assert run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1070)) == 1
    assert run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1100)) == 0
    [msg] = discord.sent
    embed = msg["embeds"][0]
    assert embed["title"] == "truenas nie działa" and embed["url"] == f"https://denali.lan/incydenty/{iid}"
    assert "**Przyczyna:** truenas" in embed["description"]
    assert "Przez to nie działa:** Immich — to skutek" in embed["description"]
    assert msg["allowed_mentions"] == {"parse": []}                  # bez @everyone
    incidents.recover(conn, truenas, now=1200)
    assert run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1210)) == 1
    assert discord.sent[1]["embeds"][0]["title"] == "Rozwiązane: truenas nie działa"
    assert run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1300)) == 0


def test_zajmuje_sie_tym_przed_wyslaniem_wycisza(conn, app):
    discord = FakeDiscord()
    iid, truenas = lab(conn, app)
    incidents.ack(conn, iid, "jakub", now=1010)
    run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1100))
    incidents.recover(conn, truenas, now=1200)
    run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1300))
    assert discord.sent == []


def test_profil_bez_powiadomien(conn, app):
    discord = FakeDiscord()
    iid, truenas = lab(conn, app)
    profiles.set_override(conn, truenas, "notify", False)
    run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1100))
    assert discord.sent == []


def test_prog_waznosci(conn, app):
    discord = FakeDiscord()
    iid, _ = lab(conn, app, severity_p1=False, min_severity="krytyczny")   # incydent „ostrzeżenie”
    run(notify.process(conn, app.state.box, "https://denali.lan", discord.transport, now=1100))
    assert discord.sent == []


def test_blad_discorda_zapisany_i_ponowienie(conn, app):
    iid, _ = lab(conn, app)
    broken = FakeDiscord(status=404)
    assert run(notify.process(conn, app.state.box, "https://denali.lan", broken.transport, now=1100)) == 0
    ch = conn.execute(select(notification_channel)).mappings().first()
    assert "odrzucił webhook" in ch["last_error"]
    assert conn.execute(select(incident.c.notified_at)).scalar() is None       # spróbuje ponownie
    ok = FakeDiscord()
    assert run(notify.process(conn, app.state.box, "https://denali.lan", ok.transport, now=1160)) == 1


def test_webhook_zaszyfrowany_i_sprawdzony(conn, app):
    with pytest.raises(notify.NotifyError, match="webhooka Discorda"):
        notify.save_channel(conn, app.state.box, "x", "https://evil.example/api/webhooks/1/2")
    notify.save_channel(conn, app.state.box, "Discord", WEBHOOK)
    ct = conn.execute(select(notification_channel.c.config_ct)).scalar()
    assert "abc-tajne" not in ct


def test_dodanie_kanalu_w_panelu(app):
    discord = FakeDiscord()
    app.state.notify_transport = discord.transport
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        page = c.get("/admin/powiadomienia").text
        r = c.post("/admin/powiadomienia", data={"csrf_token": csrf_from(page), "name": "Discord",
                                                  "webhook": WEBHOOK, "min_severity": "ostrzezenie"})
        assert r.status_code == 200 and "wiadomość testowa" in r.text
        assert "abc-tajne" not in r.text
    assert discord.sent[0]["embeds"][0]["title"] == "Test powiadomień Denali"
