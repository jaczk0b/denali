"""Gdy potrzeba hasła, panel od razu o nie pyta, a po potwierdzeniu sam dokańcza kliknięte działanie."""
import re

from denali import auth, integrations
from denali.db import kv_get
from denali.web.deps import PENDING_VALID, RESUME_PATH

from .conftest import ADMIN_PW, csrf_from, user_row
from .test_akcje import admin, allow, fresh_reauth, pve, pve_actions  # noqa: F401
from .test_proxmox import CONFIG


def confirm(app, client, location):
    """Okno z hasłem: to, co pokaże przeglądarka po przekierowaniu."""
    assert location == f"/potwierdz?next={RESUME_PATH}"
    page = client.get(location).text
    assert 'name="password"' in page
    step = user_row(app)["totp_last_step"] or 0
    r = client.post("/potwierdz", data={"csrf_token": csrf_from(page), "password": ADMIN_PW, "next": RESUME_PATH,
                                        "code": auth.totp_code(client.secret, step + 1)})
    assert r.status_code == 303 and r.headers["location"] == RESUME_PATH
    return client.get(RESUME_PATH)


def replay(client, page):
    """To, co robi denali.js na stronie dokończenia: wysyła formularz bez zmian."""
    url, body = re.search(r'<form[^>]*action="([^"]+)" data-autosubmit>(.*?)</form>', page.text, re.S).groups()
    fields = {}
    for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body):
        fields.setdefault(name, []).append(value.replace("&#34;", '"').replace("&#39;", "'"))
    return client.post(url, data=fields)


def test_zmiana_adresu_pyta_o_haslo_i_zapisuje_bez_ponownego_wypelniania(app, pve, admin):
    url = f"/admin/integracje/{pve['iid']}"
    page = admin.get(url).text
    r = admin.post(url, data={"csrf_token": csrf_from(page), "type": "proxmox", "name": "pve", "url": "https://nowy.host:8006",
                              "token_id": CONFIG["token_id"], "token_secret": "NOWY-SEKRET", "verify_tls": "1"})
    assert r.status_code == 303
    with app.state.engine.connect() as conn:
        assert integrations.get(conn, pve["iid"])["config"]["url"] == CONFIG["url"]     # jeszcze nic nie zapisane
        raw = kv_get(conn, f"reauth.pending.{user_row(app)['id']}")
    assert "NOWY-SEKRET" not in raw and "nowy.host" not in raw                          # czeka zaszyfrowany
    done = replay(admin, confirm(app, admin, r.headers["location"]))
    assert done.status_code == 303
    with app.state.engine.connect() as conn:
        row = integrations.get(conn, pve["iid"])
        assert row["config"]["url"] == "https://nowy.host:8006"
        assert integrations.secrets_of(row, app.state.box)["token_secret"] == "NOWY-SEKRET"
        assert kv_get(conn, f"reauth.pending.{user_row(app)['id']}") is None


def test_wielokrotne_pola_wracaja_w_calosci(app, pve, admin):
    url = f"/admin/integracje/{pve['iid']}/uprawnienia"
    page = admin.get(f"/admin/integracje/{pve['iid']}").text
    r = admin.post(url, data={"csrf_token": csrf_from(page), "grant": ["akcje", "konsola"]})
    replay(admin, confirm(app, admin, r.headers["location"]))
    with app.state.engine.connect() as conn:
        assert integrations.get(conn, pve["iid"])["config"]["permissions"] == ["akcje", "konsola"]


def test_niebezpieczna_akcja_dochodzi_do_skutku_po_hasle(app, pve, admin):
    app.state.adapter_transport = pve_actions()
    ref_id, dev = pve["docker"]
    with app.state.engine.begin() as conn:
        allow(conn, ["proxmox.reboot"])
    url = f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.reboot"
    r = admin.post(url, data={"csrf_token": csrf_from(admin.get(url).text)})
    done = replay(admin, confirm(app, admin, r.headers["location"]))
    assert done.status_code == 303 and done.headers["location"].startswith("/akcje/")


def test_akcja_zablokowana_nie_pyta_o_haslo(app, pve, admin):
    ref_id, dev = pve["docker"]
    url = f"/urzadzenia/{dev}/akcje/{ref_id}/proxmox.reboot"
    r = admin.post(url, data={"csrf_token": csrf_from(admin.get(url).text)})
    assert r.status_code == 403 and "Niedozwolona" in r.text


def test_formularze_widac_bez_potwierdzenia(app, pve, admin):
    assert "akcje-dozwolone" in admin.get(f"/urzadzenia/{pve['docker'][1]}").text
    assert 'action="/admin/profile/' in admin.get("/admin/profile").text
    assert "Potwierdź hasłem i kodem" not in admin.get("/admin/profile").text


def test_dokonczenie_jest_jednorazowe_i_wygasa(app, pve, admin, monkeypatch):
    url = f"/admin/integracje/{pve['iid']}/uprawnienia"
    page = admin.get(f"/admin/integracje/{pve['iid']}").text
    admin.post(url, data={"csrf_token": csrf_from(page), "grant": ["akcje", "konsola"]})
    fresh_reauth(app, admin)
    admin_get = admin.get(RESUME_PATH)
    assert "data-autosubmit" in admin_get.text
    assert admin.get(RESUME_PATH).headers["location"] == "/"                # drugi raz nic się nie wykona
    # po czasie zapamiętane działanie przepada
    with app.state.engine.begin() as conn:
        auth.mark_reauth(conn, admin.cookies.get("denali_session"), 0)
    admin.post(url, data={"csrf_token": csrf_from(page), "grant": ["konsola"]})
    fresh_reauth(app, admin)
    import denali.web.deps as deps
    now = deps.time.time() + PENDING_VALID + 1
    monkeypatch.setattr(deps.time, "time", lambda: now)
    with app.state.engine.begin() as conn:
        assert deps.take_pending(conn, app.state.box, auth.load_session(conn, admin.cookies.get("denali_session"), now=now - 1)) is None


def test_dokonczenie_bez_hasla_wraca_do_okna(app, pve, admin):
    assert admin.get(RESUME_PATH).headers["location"].startswith("/potwierdz")
