"""Tablet przy szafie: ściana z podglądem, PIN ustawiany w panelu, odblokowanie z limitem czasu, ustawienia tabletu."""
import re

import pytest

from denali import auth, tablet
from denali.db import kv_get, kv_set
from denali.models import audit_log

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client
from .test_notify import WEBHOOK, FakeDiscord
from .test_widoki import pair, pair_code

PIN = "482916"


@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


@pytest.fixture
def pad(app, admin):
    """Połączony tablet z PIN-em ustawionym przez administratora."""
    c, _ = pair(app, pair_code(admin))
    page = admin.get("/admin/tablet").text
    public_id = re.search(r'/admin/tablet/([0-9a-f]+)/pin"', page).group(1)
    with app.state.engine.begin() as conn:
        auth.mark_reauth(conn, admin.cookies.get("denali_session"))
    r = admin.post(f"/admin/tablet/{public_id}/pin", data={"csrf_token": csrf_from(page), "pin": PIN})
    assert "PIN ustawiony" in r.text
    c.public_id = public_id
    return c


def unlock(c, pin=PIN):
    page = c.get("/").text
    return c.post("/tablet/odblokuj", data={"csrf_token": csrf_from(page), "pin": pin})


def test_sciana_to_podglad(app, admin):
    c, _ = pair(app, pair_code(admin))
    page = c.get("/").text
    assert "wall-body wall-panel" in page and "Bez PIN-u — tylko podgląd" in page
    assert "Gdzie jest problem" in page and "Ustawienia tego tabletu" in page
    assert c.get("/terminale").status_code == 403
    assert c.post("/tablet/odblokuj", data={"csrf_token": csrf_from(page), "pin": PIN}).status_code == 303
    assert "nie ma PIN-u" in c.get("/").text


def test_pin_wymaga_potwierdzenia_i_odrzuca_oczywiste(app, admin):
    pair(app, pair_code(admin))
    page = admin.get("/admin/tablet").text
    public_id = re.search(r'/admin/tablet/([0-9a-f]+)/pin"', page).group(1)
    r = admin.post(f"/admin/tablet/{public_id}/pin", data={"csrf_token": csrf_from(page), "pin": PIN})
    assert r.status_code == 303 and r.headers["location"].startswith("/potwierdz")       # najpierw hasło i 2FA
    with app.state.engine.begin() as conn:
        auth.mark_reauth(conn, admin.cookies.get("denali_session"))
        for bad in ("1234", "111111", "123456", "987654", "12a456"):
            with pytest.raises(tablet.TabletError):
                tablet.set_pin(conn, public_id, bad, "admin")
        tablet.set_pin(conn, public_id, PIN, "admin")
        assert kv_get(conn, f"tablet.pin.{public_id}")["hash"].startswith("$argon2")      # nie sam PIN


def test_odblokowanie_daje_terminale_i_wygasa(app, pad):
    assert pad.get("/terminale").status_code == 403
    assert unlock(pad).headers["location"] == "/"
    wall = pad.get("/").text
    assert "Odblokowany · <b>admin</b>" in wall and 'href="/terminale"' in wall
    assert pad.get("/terminale").status_code == 200
    assert "Tablet odblokowany jako <b>admin</b>" in pad.get("/terminale").text
    with app.state.engine.begin() as conn:
        rows = conn.execute(audit_log.select().where(audit_log.c.action == "tablet_unlock")).all()
        assert rows and rows[0].user == "admin"
        # 10 minut bez dotyku — z powrotem na ścianę
        key = f"tablet.unlock.{pad.public_id}"
        u = kv_get(conn, key)
        kv_set(conn, key, {**u, "last": u["last"] - tablet.UNLOCK_IDLE - 1})
    assert pad.get("/terminale").status_code == 403
    assert "Odblokuj" in pad.get("/").text


def test_zablokuj_i_najdluzej_godzine(app, pad):
    unlock(pad)
    page = pad.get("/").text
    pad.post("/tablet/zablokuj", data={"csrf_token": csrf_from(page)})
    assert pad.get("/terminale").status_code == 403
    unlock(pad)
    with app.state.engine.begin() as conn:
        key = f"tablet.unlock.{pad.public_id}"
        u = kv_get(conn, key)
        kv_set(conn, key, {**u, "at": u["at"] - tablet.UNLOCK_MAX - 1})       # aktywny, ale minęła godzina
    assert pad.get("/terminale").status_code == 403


def test_zle_piny_kasuja_pin_i_powiadamiaja(app, pad):
    discord = FakeDiscord()
    app.state.notify_transport = discord.transport
    from denali import notify
    with app.state.engine.begin() as conn:
        notify.save_channel(conn, app.state.box, "Discord", WEBHOOK)
    for i in range(tablet.MAX_FAILS - 1):
        unlock(pad, "000001")
    assert "Zostało prób: 1" in pad.get("/").text
    unlock(pad, "000001")
    assert "PIN skasowany" in pad.get("/").text
    assert unlock(pad).status_code == 303 and pad.get("/terminale").status_code == 403    # dobry PIN już nie działa
    assert discord.sent and "PIN skasowany" in discord.sent[0]["embeds"][0]["title"]


def test_poprawny_pin_zeruje_licznik(app, pad):
    unlock(pad, "000001")
    unlock(pad)
    with app.state.engine.connect() as conn:
        assert tablet.pin_info(conn, pad.public_id)["fails"] == 0


def test_zwykla_sesja_nie_odblokowuje(app, admin):
    page = admin.get("/konto").text
    assert admin.post("/tablet/odblokuj", data={"csrf_token": csrf_from(page), "pin": PIN}).status_code == 403


def test_ustawienia_tabletu(app, admin):
    c, _ = pair(app, pair_code(admin))
    page = c.get("/").text
    c.post("/tablet/ustawienia", data={"csrf_token": csrf_from(page), "mode": "ciemny", "quiet_from": "23",
                                       "quiet_to": "6"})
    page = c.get("/").text
    assert "wall-ciemny" in page and 'data-quiet-from="23"' in page and 'data-quiet-to="6"' in page
    assert tablet.prefs("zle;99;x") == {"mode": "panel", "quiet_from": 22, "quiet_to": 7}
