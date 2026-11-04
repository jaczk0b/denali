"""Integracja OVHcloud: podpis zapytań, termin opłaty, stan VPS-a i zakładka VPS (na nagranym API, bez konta OVH)."""
import asyncio
import hashlib
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from denali import consoles, discovery, integrations
from denali.adapters import AdapterError
from denali.adapters.ovh import Ovh, payment
from denali.models import device

from .conftest import do_login, enable_2fa, make_user, new_client

CONFIG = {"endpoint": "ovh-eu", "application_key": "ak"}
SECRETS = {"application_secret": "as", "consumer_key": "ck"}
BASE = "https://eu.api.ovh.com/1.0"
NAME = "vps-abc.vps.ovh.net"
TODAY = date(2026, 11, 4)


def service(days=30, automatic=True, status="ok", **renew):
    end = date.today() + timedelta(days=days)
    return {"status": status, "expiration": end.isoformat(), "creation": "2025-01-10", "engagedUpTo": None,
            "contactBilling": "nic-ovh", "renew": {"automatic": automatic, "manualPayment": False, **renew}}


def ovh_api(state="running", svc=None, billing=True, seen=None):
    routes = {
        "/vps": [NAME],
        f"/vps/{NAME}": {"name": NAME, "displayName": "vps-pangolin", "state": state, "vcore": 2, "memoryLimit": 4096,
                         "zone": "Region OpenStack: os-waw", "lockStatus": {"locked": False, "reason": None},
                         "model": {"name": "vps-le-2-4-80", "offer": "VPS Le 2-4-80", "disk": 80}},
        f"/vps/{NAME}/serviceInfos": svc or service(),
        f"/vps/{NAME}/option": ["automatedBackup"],
        f"/vps/{NAME}/ips": ["2001:db8::1", "51.68.1.2"],
        f"/vps/{NAME}/datacenter": {"longName": "Warsaw", "name": "waw"},
        f"/vps/{NAME}/automatedBackup": {"state": "enabled", "schedule": "03:00:00"},
    }
    if billing:
        routes |= {"/me/debtAccount": {"dueAmount": {"text": "0.00 PLN", "value": 0}},
                   "/me/bill": ["PL100", "PL101"],
                   "/me/bill/PL100": {"billId": "PL100", "date": "2026-09-01T10:00:00+02:00",
                                      "priceWithTax": {"text": "30.00 PLN"}, "pdfUrl": "https://x/?reference=tajne"},
                   "/me/bill/PL101": {"billId": "PL101", "date": "2026-10-01T10:00:00+02:00",
                                      "priceWithTax": {"text": "31.00 PLN"}, "pdfUrl": "https://x/?reference=tajne"}}

    def handler(request):
        path = request.url.path.removeprefix("/1.0")
        if path == "/auth/time":
            return httpx.Response(200, json=1_800_000_000)
        if seen is not None:
            seen.append(request)
        if path in routes:
            return httpx.Response(200, json=routes[path])
        if path.startswith("/me"):
            return httpx.Response(403, json={"errorCode": "NOT_GRANTED_CALL", "message": "nie"})
        return httpx.Response(404, json={"message": "nie ma"})
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def adapter(transport):
    return Ovh(CONFIG, SECRETS, transport=transport)


def test_podpis_zgodny_z_dokumentacja_ovh():
    seen = []
    run(adapter(ovh_api(seen=seen)).test())
    r = next(x for x in seen if str(x.url).endswith("/me/debtAccount"))
    ts = r.headers["X-Ovh-Timestamp"]
    assert abs(int(ts) - 1_800_000_000) < 60    # czas z /auth/time, nie z komputera
    expected = hashlib.sha1(f"as+ck+GET+{BASE}/me/debtAccount++{ts}".encode()).hexdigest()
    assert r.headers["X-Ovh-Signature"] == f"$1${expected}"
    assert (r.headers["X-Ovh-Application"], r.headers["X-Ovh-Consumer"]) == ("ak", "ck")


def test_podpis_obejmuje_parametry_zapytania():
    seen = []
    run(adapter(ovh_api(seen=seen)).test())
    r = next(x for x in seen if x.url.path.endswith("/me/bill"))
    ts = r.headers["X-Ovh-Timestamp"]
    assert "date.from=" in str(r.url)
    assert r.headers["X-Ovh-Signature"] == "$1$" + hashlib.sha1(f"as+ck+GET+{r.url}++{ts}".encode()).hexdigest()


@pytest.mark.parametrize("svc,state,text", [
    ({"status": "ok", "expiration": "2026-12-20", "renew": {"automatic": True}}, "ok", "odnowienie automatyczne 20.12.2026"),
    ({"status": "ok", "expiration": "2026-11-06", "renew": {"automatic": True}}, "ok", "odnowienie automatyczne 06.11.2026"),
    ({"status": "ok", "expiration": "2026-12-20", "renew": {"automatic": False}}, "ok", "zapłać do 20.12.2026 (za 46 dni)"),
    ({"status": "ok", "expiration": "2026-11-10", "renew": {"automatic": False}}, "warn", "zapłać do 10.11.2026 (za 6 dni)"),
    ({"status": "ok", "expiration": "2026-11-10", "renew": {"automatic": True, "manualPayment": True}}, "warn",
     "zapłać do 10.11.2026 (za 6 dni)"),
    ({"status": "ok", "expiration": "2026-11-10", "renew": {"automatic": True, "deleteAtExpiration": True}}, "warn",
     "zostanie usunięty 10.11.2026 (za 6 dni)"),
    ({"status": "unPaid", "expiration": "2026-11-10", "renew": {}}, "warn", "nieopłacony, termin 10.11.2026"),
    ({"status": "expired", "expiration": "2026-11-01", "renew": {}}, "down", "wygasł 01.11.2026"),
    ({"status": "ok", "expiration": "2026-11-01", "renew": {"automatic": True}}, "down", "wygasł 01.11.2026"),
    ({"status": "autorenewInProgress", "expiration": "2026-11-01", "renew": {"automatic": True}}, "ok",
     "odnowienie w toku (termin 01.11.2026)"),
    ({"status": "ok", "expiration": None, "renew": {}}, "warn", "OVH nie podał terminu opłaty"),
])
def test_termin_oplaty(svc, state, text):
    assert payment(svc, TODAY) == (state, text)


def test_odczyt_vps_i_konta():
    out = run(adapter(ovh_api()).poll())
    found = out.found[0]
    assert found.external_id == f"vps/{NAME}"
    assert found.proposed["kind"] == "vps" and found.proposed["name"] == "vps-pangolin"
    assert found.proposed["addresses"][0] == {"kind": "ip", "value": "51.68.1.2"}   # SSH najpierw po IPv4
    assert {"kind": "hostname", "value": NAME} in found.proposed["addresses"]
    vps = next(s for s in out.states if s.external_id == found.external_id)
    assert vps.state == "ok" and vps.summary.startswith("działa · odnowienie automatyczne")
    assert vps.data["location"] == "Warsaw" and vps.data["backup"] is True
    account = next(s for s in out.states if s.external_id == "konto")
    assert account.state == "ok" and [b["id"] for b in account.data["bills"]] == ["PL101", "PL100"]
    assert "tajne" not in str(account.data)     # adres PDF niesie klucz do faktury


@pytest.mark.parametrize("vps_state,expected", [("stopped", "down"), ("rescued", "warn"), ("maintenance", "warn")])
def test_stan_maszyny(vps_state, expected):
    assert run(adapter(ovh_api(vps_state)).poll()).states[0].state == expected


def test_gorszy_stan_wygrywa():
    out = run(adapter(ovh_api(svc=service(days=-2))).poll())
    assert out.states[0].state == "down" and out.states[0].summary.startswith("działa · wygasł")


def test_bez_uprawnien_do_rachunkow_dziala_dalej():
    out = run(adapter(ovh_api(billing=False)).poll())
    assert len(out.found) == 1 and not any(s.external_id == "konto" for s in out.states)
    assert "brak uprawnień" in run(adapter(ovh_api(billing=False)).test())


def test_bledy():
    def denied(request):
        if request.url.path.endswith("/auth/time"):
            return httpx.Response(200, json=1_800_000_000)
        return httpx.Response(403, json={"errorCode": "INVALID_CREDENTIAL", "message": "This credential is not valid"})
    with pytest.raises(AdapterError, match="odrzuciło klucze"):
        run(adapter(httpx.MockTransport(denied)).test())

    def empty(request):
        return httpx.Response(200, json=1_800_000_000 if request.url.path.endswith("/auth/time") else [])
    with pytest.raises(AdapterError, match="nie ma VPS"):
        run(adapter(httpx.MockTransport(empty)).test())
    with pytest.raises(AdapterError, match="Region API"):
        Ovh.validate({**CONFIG, "endpoint": "gdzies"}, SECRETS)


def test_zakladka_vps_i_terminal(app):
    make_user(app, "admin")
    cli = new_client(app)
    do_login(cli, "admin")
    enable_2fa(cli, app, "admin")
    assert cli.get("/infrastruktura/vps").status_code == 303     # bez OVH nie ma zakładki
    with app.state.engine.begin() as c:
        row = integrations.get(c, integrations.save(c, app.state.box, "ovh", "OVH", CONFIG, SECRETS))
    assert run(integrations.poll_one(app.state.engine, app.state.box, row, ovh_api())) is None
    html = cli.get("/infrastruktura/vps").text
    assert "vps-pangolin" in html and "Warsaw" in html and "51.68.1.2" in html and "PL101" in html
    assert "Zatwierdź, by mieć terminal" in html and 'href="/infrastruktura/vps"' in html
    with app.state.engine.begin() as c:
        ref = next(r for r in discovery.pending(c) if r["external_id"] == f"vps/{NAME}")
        discovery.approve(c, ref["id"], "admin")
    html = cli.get("/infrastruktura/vps").text
    assert 'href="/terminale/' in html and "Karta urządzenia" in html
    with app.state.engine.connect() as c:
        device_id = c.execute(select(device.c.id).where(device.c.kind == "vps")).scalar_one()
        assert consoles.addresses(c, device_id)[0] == "51.68.1.2"
    assert cli.get(f"/terminale/{device_id}").status_code == 200
    assert "Dodaj: OVHcloud" in cli.get("/admin/integracje").text
    form = cli.get("/admin/integracje/nowa?typ=ovh").text
    assert "createToken" in form and "ovh-eu" in form
    page = cli.get(f"/admin/integracje/{row['id']}")     # stan „konto” nie ma urządzenia ani wpisu w kolejce
    assert page.status_code == 200 and "bez zaległości" in page.text
