"""Interfejs: każda strona się renderuje, uprawnienia, formularze, skarbiec, tablet, bezpieczny Markdown."""
import re

import pytest
from sqlalchemy import select

from denali import auth, discovery, docs, incidents, inventory as inv
from denali.models import audit_log, device, integration, profile

from .conftest import ADMIN_PW, csrf_from, do_login, enable_2fa, make_user, new_client, user_row


@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        c.secret = enable_2fa(c, app)
        yield c


@pytest.fixture
def viewer(app):
    make_user(app, role="podglad", login="widz")
    with new_client(app) as c:
        do_login(c, login="widz")
        yield c


@pytest.fixture
def lab(app):
    """Scenariusz z makiety: TrueNAS leży, Immich to skutek."""
    with app.state.engine.begin() as conn:
        ids = {"pve": inv.create_device(conn, "pve", "host", criticality=1)}
        ids["truenas"] = inv.create_device(conn, "truenas", "vm", parent_id=ids["pve"], criticality=1,
                                           pulpit_group="zarzadzanie", app_url="https://truenas.lan")
        ids["docker"] = inv.create_device(conn, "docker", "vm", parent_id=ids["pve"])
        ids["immich"] = inv.create_device(conn, "Immich", "kontener", parent_id=ids["docker"],
                                          pulpit_group="codzien", app_url="https://immich.lan")
        inv.add_dependency(conn, ids["immich"], ids["truenas"], "dane")
        inv.add_address(conn, ids["truenas"], "ip", "192.168.1.30")
        g = inv.load_graph(conn)
        [f] = g.analyze({i: "ok" for i in ids.values()} | {ids["truenas"]: "down", ids["immich"]: "down"})
        ids["incident"], _ = incidents.record_finding(conn, g, f)
        ids["procedure"] = docs.save_procedure(conn, "Padł TrueNAS", [("Sprawdź konsolę VM 100", "w Proxmoxie")],
                                               trigger="device_down:truenas")
        ids["runbook"] = docs.save_runbook(conn, "Restart TrueNAS", "Najpierw **eksport puli**.", [ids["truenas"]])
        i = conn.execute(integration.insert().values(type="komodo", name="komodo")).inserted_primary_key[0]
        ids["ref"], _ = discovery.upsert(conn, i, "stack/jellyfin", {"name": "Jellyfin", "kind": "kontener",
                                                                     "parent": {"ip": "10.0.0.9"}})
    return ids


def post(client, url, page_url=None, **data):
    page = client.get(page_url or "/konto").text
    return client.post(url, data={"csrf_token": csrf_from(page), **data})


def test_kazda_strona_sie_renderuje(admin, lab):
    pages = ["/", "/urzadzenia", f"/urzadzenia/{lab['truenas']}", f"/urzadzenia/{lab['immich']}",
             "/urzadzenia/nowe", f"/urzadzenia/{lab['pve']}/edycja", "/zatwierdz", "/incydenty",
             "/incydenty?wszystkie=1", f"/incydenty/{lab['incident']}", "/incydenty/nowy", "/procedury",
             f"/procedury/{lab['procedure']}", "/procedury/nowa", f"/procedury/{lab['procedure']}/edycja",
             f"/runbooki/{lab['runbook']}", "/runbooki/nowy", f"/runbooki/{lab['runbook']}/edycja",
             "/szukaj?q=truenas", "/admin/profile", "/admin/tablet", "/potwierdz", "/admin/audyt"]
    for p in pages:
        r = admin.get(p)
        assert r.status_code == 200, p


def test_pulpit_pokazuje_przyczyne_i_skutki(admin, lab):
    html = admin.get("/").text
    assert "truenas nie działa" in html and "Co się stało" in html
    assert "<b>Serwery · truenas</b><span>tu jest przyczyna</span>" in html   # truenas to VM, więc warstwa „Serwery”
    assert "bez danych" in html and "Skutek, nie osobna awaria" in html   # ikona Immich: skutek, nie osobna awaria
    assert 'href="https://immich.lan"' in html and 'rel="noopener noreferrer"' in html


def test_karta_urzadzenia(admin, lab):
    html = admin.get(f"/urzadzenia/{lab['truenas']}").text
    assert "przestanie działać</strong> Immich" in html
    assert "Padł TrueNAS" in html and "Restart TrueNAS" in html          # procedura z wyzwalacza + runbook
    assert "Maszyna wirtualna" in html                                    # profil po typie
    assert "192.168.1.30" in html


def test_brak_urzadzenia_to_404(admin):
    assert admin.get("/urzadzenia/999").status_code == 404
    assert admin.get("/incydenty/999").status_code == 404


def test_podglad_widzi_ale_nie_zmienia(viewer, lab):
    assert viewer.get(f"/urzadzenia/{lab['truenas']}").status_code == 200
    assert "Skarbiec" not in viewer.get(f"/urzadzenia/{lab['truenas']}").text
    for url in ("/urzadzenia/nowe", "/incydenty/nowy", "/procedury/nowa", "/admin/tablet", "/admin/profile"):
        assert viewer.get(url).status_code == 403, url
    r = post(viewer, "/urzadzenia/nowe", name="x", kind="host")
    assert r.status_code == 403
    r = post(viewer, f"/zatwierdz/{lab['ref']}")
    assert r.status_code == 403


def test_zmiany_wymagaja_csrf(admin, lab):
    r = admin.post("/urzadzenia/nowe", data={"name": "x", "kind": "host"})
    assert r.status_code == 400 and "Formularz wygasł" in r.text


def test_nowe_urzadzenie_z_formularza(admin, app):
    r = post(admin, "/urzadzenia/nowe", "/urzadzenia/nowe", name="ups", kind="ups")
    assert r.status_code == 303
    r = admin.get(r.headers["location"])
    assert "<h1>ups" in r.text
    r = post(admin, "/urzadzenia/nowe", "/urzadzenia/nowe", name="", kind="ups")
    assert r.status_code == 400 and "Podaj nazwę" in r.text


def test_petla_pokazana_jako_blad(admin, lab):
    r = post(admin, f"/urzadzenia/{lab['pve']}/zaleznosci", depends_on_id=str(lab["immich"]), type="dane", hard="1")
    assert r.status_code == 400 and "pętlę" in r.text


def test_wyjatek_od_profilu(admin, lab):
    url = f"/urzadzenia/{lab['docker']}"
    post(admin, f"{url}/ustawienia", key="notify", value="nie")
    assert "inne niż profil" in admin.get(url).text
    post(admin, f"{url}/ustawienia", key="notify", clear="1")
    assert "inne niż profil" not in admin.get(url).text
    r = post(admin, f"{url}/ustawienia", key="actions", value="restart")
    assert r.status_code == 400                                          # akcji nie ustawia się z karty


def test_profil_dla_wszystkich(admin, lab, app):
    with app.state.engine.connect() as conn:
        pid = conn.execute(select(profile.c.id).where(profile.c.name == "Maszyna wirtualna")).scalar_one()
    post(admin, f"/admin/profile/{pid}", "/admin/profile", key="down_after_s", value="300")
    assert "300" in admin.get(f"/urzadzenia/{lab['docker']}").text


def test_plik_zawsze_jako_zalacznik(admin, lab):
    page = admin.get(f"/urzadzenia/{lab['pve']}").text
    r = admin.post(f"/urzadzenia/{lab['pve']}/pliki", data={"csrf_token": csrf_from(page)},
                   files={"plik": ("zly.html", b"<script>alert(1)</script>", "text/html")})
    assert r.status_code == 303
    fid = re.search(r'href="/pliki/(\d+)"', admin.get(f"/urzadzenia/{lab['pve']}").text).group(1)
    r = admin.get(f"/pliki/{fid}")
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_skarbiec_wymaga_potwierdzenia(admin, lab, app):
    url = f"/urzadzenia/{lab['pve']}"
    post(admin, f"{url}/skarbiec", url, label="iDRAC", username="root", secret="Bardzo-Tajne-1")
    page = admin.get(url).text
    assert "Bardzo-Tajne-1" not in page
    sid = re.search(r'/skarbiec/(\d+)/odslon', page).group(1)
    r = post(admin, f"{url}/skarbiec/{sid}/odslon", url)
    assert r.status_code == 303 and r.headers["location"].startswith("/potwierdz")

    form = admin.get("/potwierdz").text
    step = user_row(app)["totp_last_step"]
    r = admin.post("/potwierdz", data={"csrf_token": csrf_from(form), "password": "zle-haslo-zle-haslo",
                                       "code": auth.totp_code(admin.secret, step + 1), "next": url})
    assert r.status_code == 400
    r = admin.post("/potwierdz", data={"csrf_token": csrf_from(form), "password": ADMIN_PW,
                                       "code": auth.totp_code(admin.secret, step + 1), "next": url})
    assert r.status_code == 303 and r.headers["location"] == url
    r = post(admin, f"{url}/skarbiec/{sid}/odslon", url)
    assert r.status_code == 200 and "Bardzo-Tajne-1" in r.text
    with app.state.engine.connect() as conn:
        actions = [a for (a,) in conn.execute(select(audit_log.c.action))]
    assert "reauth" in actions and "secret_reveal" in actions and "reauth_failed" in actions


def test_zatwierdzenie_z_kolejki(admin, lab, app):
    assert "Jellyfin" in admin.get("/zatwierdz").text
    r = post(admin, f"/zatwierdz/{lab['ref']}", "/zatwierdz")
    assert r.status_code == 303
    with app.state.engine.connect() as conn:
        assert conn.execute(select(device.c.kind).where(device.c.name == "Jellyfin")).scalar_one() == "kontener"
    assert "Nic nie czeka" in admin.get("/zatwierdz").text


def test_incydent_reczny_od_poczatku_do_konca(admin, lab):
    r = post(admin, "/incydenty/nowy", "/incydenty/nowy", title="Wolny Jellyfin", device_id=str(lab["docker"]),
             severity="info", text="wieczorem")
    url = r.headers["location"]
    post(admin, f"{url}/potwierdz", url)
    post(admin, f"{url}/notatka", url, text="transkodowanie na CPU")
    post(admin, f"{url}/zamknij", url, cause_md="brak GPU", fix_md="włączone QSV")
    html = admin.get(url).text
    assert "zamknięty</span>" in html and "transkodowanie na CPU" in html and "włączone QSV" in html


def test_procedura_z_formularza(admin, lab):
    page = admin.get("/procedury/nowa").text
    r = admin.post("/procedury/nowa", data={
        "csrf_token": csrf_from(page), "title": "Padł AP", "scope": "infrastruktura", "trigger": "device_down:typ:ap",
        "intro_md": "", "step_title": ["Sprawdź PoE", "", "Restart AP"], "step_body": ["na switchu", "", ""],
        "device_ids": [str(lab["pve"])]})
    assert r.status_code == 303
    html = admin.get(r.headers["location"]).text
    assert "Sprawdź PoE" in html and "Restart AP" in html and 'class="step-no">2<' in html


def test_markdown_bez_html_i_javascript(admin, lab):
    page = admin.get(f"/runbooki/{lab['runbook']}/edycja").text
    admin.post(f"/runbooki/{lab['runbook']}/edycja", data={
        "csrf_token": csrf_from(page), "title": "R", "device_ids": [str(lab["truenas"])],
        "body_md": "<script>alert(1)</script>\n\n[klik](javascript:alert(1)) **ok**"})
    html = admin.get(f"/runbooki/{lab['runbook']}").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
    assert 'href="javascript' not in html
    assert "<strong>ok</strong>" in html


def test_wyszukiwarka(admin, lab):
    assert "truenas" in admin.get("/szukaj?q=192.168.1.30").text
    assert "Padł TrueNAS" in admin.get("/szukaj?q=konsol").text
    assert "Nic nie znaleziono" in admin.get("/szukaj?q=%25").text       # % to zwykły znak, nie wildcard


def test_usuniecie_wymaga_przepisania_nazwy(admin, lab):
    url = f"/urzadzenia/{lab['immich']}"
    assert post(admin, f"{url}/usun", url, confirm="immich").status_code == 400
    assert post(admin, f"{url}/usun", url, confirm="Immich").status_code == 303
    assert admin.get(url).status_code == 404


# ───────────────────────── tablet ─────────────────────────
def pair_code(admin):
    r = post(admin, "/admin/tablet/kod", "/admin/tablet")
    return re.search(r'class="pair-code mono">([A-Z0-9-]+)<', r.text).group(1)


def pair(app, code):
    c = new_client(app)
    c.__enter__()
    c.get("/tablet")
    r = c.post("/tablet", data={"code": code, "pre": c.cookies.get("denali_prelogin")})
    return c, r


def test_tablet_laczy_sie_kodem_jednorazowym(admin, app):
    code = pair_code(admin)
    tablet, r = pair(app, code.lower())                # wielkość liter i myślnik bez znaczenia
    assert r.status_code == 303
    home = tablet.get("/")
    assert home.status_code == 200 and 'http-equiv="refresh"' in home.text
    assert tablet.get("/urzadzenia/nowe").status_code == 403         # tylko podgląd
    _other, r = pair(app, code)
    assert r.status_code == 400                                       # kod działa raz

    public_id = re.search(r'/admin/tablet/([0-9a-f]+)/usun', admin.get("/admin/tablet").text).group(1)
    post(admin, f"/admin/tablet/{public_id}/usun", "/admin/tablet")
    assert tablet.get("/").status_code == 303                         # odłączony


def test_zly_kod_tabletu_liczy_sie_do_limitu(app):
    for _ in range(auth.MAX_FAILS_PER_LOGIN):
        pair(app, "ZLYK-OD00")
    _c, r = pair(app, "ZLYK-OD00")
    assert r.status_code == 429
