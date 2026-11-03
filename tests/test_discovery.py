"""Kolejka „do zatwierdzenia”. Kryterium z projektu: nowa VM z Dockerem w Komodo — jedno kliknięcie, zero formularzy."""
from sqlalchemy import select

from denali import discovery, inventory as inv, profiles
from denali.models import audit_log, external_ref, integration


def add_integration(conn, type_, name):
    return conn.execute(integration.insert().values(type=type_, name=name)).inserted_primary_key[0]


def report_homelab(conn):
    pve_i = add_integration(conn, "proxmox", "pve")
    komodo_i = add_integration(conn, "komodo", "komodo")
    refs = {
        "pve": discovery.upsert(conn, pve_i, "node/pve", {
            "name": "pve", "kind": "host", "addresses": [{"kind": "ip", "value": "192.168.1.10"}]})[0],
        "truenas": discovery.upsert(conn, pve_i, "qemu/100", {
            "name": "truenas", "kind": "vm", "parent": {"ref": "node/pve"},
            "addresses": [{"kind": "ip", "value": "192.168.1.30"}]})[0],
        "docker": discovery.upsert(conn, pve_i, "qemu/107", {
            "name": "docker", "kind": "vm", "parent": {"ref": "node/pve"},
            "addresses": [{"kind": "ip", "value": "192.168.1.20"}, {"kind": "mac", "value": "BC:24:11:00:00:07"}]})[0],
        "immich": discovery.upsert(conn, komodo_i, "stack/immich", {
            "name": "immich", "kind": "kontener", "parent": {"ip": "192.168.1.20"},
            "app_url": "https://immich.lan", "depends_on": [{"ip": "192.168.1.30", "type": "dane", "hard": True}]})[0],
        "jellyfin": discovery.upsert(conn, komodo_i, "stack/jellyfin", {
            "name": "jellyfin", "kind": "kontener", "parent": {"ip": "192.168.1.20"}})[0],
    }
    return refs


def dev(conn, name):
    return conn.execute(select(inv.device).where(inv.device.c.name == name)).mappings().first()


def test_nowa_vm_ze_stackami_jednym_kliknieciem(conn):
    refs = report_homelab(conn)
    created = discovery.approve(conn, refs["docker"], user="jakub")
    names = [inv.get_device(conn, d)["name"] for d in created]
    assert names[:2] == ["pve", "docker"] and sorted(names[2:]) == ["immich", "jellyfin"]
    pve, docker, immich = dev(conn, "pve"), dev(conn, "docker"), dev(conn, "immich")
    assert docker["parent_id"] == pve["id"] and immich["parent_id"] == docker["id"]
    assert immich["app_url"] == "https://immich.lan"
    assert inv.find_by_address(conn, "mac", "bc:24:11:00:00:07") == docker["id"]
    assert profiles.effective(conn, immich["id"])[0] == "Stack Dockera"     # profil sam z typu
    assert dev(conn, "truenas") is None                                     # sąsiednia VM czeka dalej
    assert [r["external_id"] for r in discovery.pending(conn)] == ["qemu/100"]
    actions = conn.execute(select(audit_log.c.action, audit_log.c.user)).all()
    assert actions.count(("device_approve", "jakub")) == 4


def test_zatwierdz_wszystko(conn):
    report_homelab(conn)
    assert len(discovery.approve_all(conn)) == 5
    assert discovery.pending(conn) == []
    assert dev(conn, "truenas")["parent_id"] == dev(conn, "pve")["id"]


def test_ponowne_zgloszenie_nie_tworzy_duplikatu(conn):
    refs = report_homelab(conn)
    discovery.approve(conn, refs["pve"])
    i = conn.execute(select(external_ref.c.integration_id).where(external_ref.c.id == refs["pve"])).scalar_one()
    rid, new = discovery.upsert(conn, i, "node/pve", {"name": "pve", "kind": "host"})
    assert rid == refs["pve"] and not new
    assert conn.execute(select(external_ref.c.status).where(external_ref.c.id == rid)).scalar_one() == "zatwierdzony"


def test_przypiecie_do_istniejacego_urzadzenia(conn):
    refs = report_homelab(conn)
    old = inv.create_device(conn, "Serwer pve (z HLC)", "host")
    discovery.link(conn, refs["pve"], old)
    assert inv.find_by_address(conn, "ip", "192.168.1.10") == old
    discovery.approve(conn, refs["truenas"])
    assert dev(conn, "truenas")["parent_id"] == old


def test_odrzucony_nie_wraca_do_kolejki(conn):
    refs = report_homelab(conn)
    discovery.ignore(conn, refs["jellyfin"])
    i = conn.execute(select(external_ref.c.integration_id).where(external_ref.c.id == refs["jellyfin"])).scalar_one()
    discovery.upsert(conn, i, "stack/jellyfin", {"name": "jellyfin", "kind": "kontener"})
    assert "stack/jellyfin" not in [r["external_id"] for r in discovery.pending(conn)]


def test_zle_dane_z_integracji_nie_psuja_kolejki(conn):
    i = add_integration(conn, "openwrt", "router")
    rid, _ = discovery.upsert(conn, i, "mac/xx", {"kind": "kasa", "addresses": [{"kind": "ip", "value": "zle"}]})
    [d] = discovery.approve(conn, rid)
    assert inv.get_device(conn, d)["name"] == "mac/xx" and inv.get_device(conn, d)["kind"] == "inne"


def test_zaleznosc_wykryta_przez_adres(conn):
    refs = report_homelab(conn)
    discovery.approve(conn, refs["truenas"])
    discovery.approve(conn, refs["immich"])
    g = inv.load_graph(conn)
    immich, truenas = dev(conn, "immich")["id"], dev(conn, "truenas")["id"]
    assert truenas in g.ancestors(immich)


def test_dopasowanie_do_urzadzen_z_hlc(conn):
    """Po migracji z HLC Proxmox zgłasza te same maszyny — kolejka podpowiada istniejące karty."""
    pve_old = inv.create_device(conn, "pve", "host")
    inv.add_address(conn, pve_old, "ip", "192.168.1.10")
    truenas_old = inv.create_device(conn, "TrueNAS", "vm", parent_id=pve_old)          # bez IP — po nazwie
    inv.create_device(conn, "docker", "vm")
    inv.create_device(conn, "docker", "lxc")                                          # dwie „docker” — niejednoznaczne
    refs = report_homelab(conn)
    m = discovery.suggest_matches(conn)
    assert m[refs["pve"]]["id"] == pve_old                 # po IP
    assert m[refs["truenas"]]["id"] == truenas_old         # po nazwie (bez względu na wielkość liter)
    assert refs["docker"] not in m                         # dwa kandydaty — decyzja człowieka
    assert discovery.link_matches(conn) == 2
    assert discovery.suggest_matches(conn) == {}
    left = {r["external_id"] for r in discovery.pending(conn)}
    assert "node/pve" not in left and "qemu/100" not in left


# ───────────────────────── usunięte w źródle ─────────────────────────
def _poll(conn, iid, found, now, states=()):
    from denali import integrations
    from denali.adapters import Found, PollResult, State
    integrations.record(conn, iid, PollResult(found=[Found(e, p) for e, p in found],
                                              states=[State(e, "ok") for e in states]), now=now)


def test_usunieta_vm_trafia_do_kolejki_i_znika_po_decyzji(conn):
    from denali.models import observed_state
    pve = add_integration(conn, "proxmox", "pve")
    vm = {"name": "stara", "kind": "vm"}
    _poll(conn, pve, [("qemu/120", vm), ("qemu/121", {"name": "nowa", "kind": "vm"}),
                      ("lxc/130", {"name": "testowy", "kind": "lxc"})], now=1000, states=["qemu/120"])
    rid = conn.execute(select(external_ref.c.id).where(external_ref.c.external_id == "qemu/120")).scalar()
    discovery.approve(conn, rid)
    lxc = conn.execute(select(external_ref.c.id).where(external_ref.c.external_id == "lxc/130")).scalar()
    discovery.approve(conn, lxc)
    did = conn.execute(select(external_ref.c.device_id).where(external_ref.c.id == rid)).scalar()

    _poll(conn, pve, [], now=1300)                              # chwilowa dziura — jeszcze nic
    assert discovery.gone(conn) == []
    _poll(conn, pve, [("lxc/130", {"name": "testowy", "kind": "lxc"})], now=1700)
    gone = discovery.gone(conn)
    assert [(g["external_id"], g["device_name"]) for g in gone] == [("qemu/120", "stara")]
    assert discovery.gone(conn, did)[0]["iname"] == "pve"
    # czekająca VM, której już nie ma, sama znika z kolejki
    assert [r["external_id"] for r in discovery.pending(conn)] == []

    assert discovery.resolve_gone(conn, {gone[0]["id"]: "usun"}, user="jakub") == 1
    assert inv.get_device(conn, did) is None
    assert not conn.execute(select(external_ref).where(external_ref.c.external_id == "qemu/120")).first()
    assert not conn.execute(select(observed_state).where(observed_state.c.external_id == "qemu/120")).first()
    assert conn.execute(select(audit_log.c.action).order_by(audit_log.c.id.desc())).scalar() == "device_delete"
    # ten sam numer VM w Proxmoxie później = nowy obiekt w kolejce, nie stare urządzenie
    _poll(conn, pve, [("qemu/120", {"name": "inna", "kind": "vm"}), ("lxc/130", {"name": "testowy", "kind": "lxc"})],
          now=1800)
    assert [r["proposed"]["name"] for r in discovery.pending(conn)] == ["inna"]


def test_zostaw_urzadzenie_bez_powiazania(conn):
    pve = add_integration(conn, "proxmox", "pve")
    _poll(conn, pve, [("lxc/130", {"name": "testowy", "kind": "lxc"})], now=1000)
    rid = conn.execute(select(external_ref.c.id)).scalar()
    discovery.approve(conn, rid)
    did = conn.execute(select(external_ref.c.device_id)).scalar()
    _poll(conn, pve, [], now=1700)
    assert discovery.resolve_gone(conn, {rid: "zostaw"}) == 1
    assert inv.get_device(conn, did)["name"] == "testowy"
    assert discovery.gone(conn) == [] and not conn.execute(select(external_ref)).first()


def test_klient_wifi_poza_siecia_to_nie_usuniecie(conn):
    router = add_integration(conn, "tplink", "router")
    _poll(conn, router, [("mac/aa:bb:cc:dd:ee:ff", {"name": "telefon", "kind": "telefon"})], now=1000)
    discovery.approve(conn, conn.execute(select(external_ref.c.id)).scalar())
    _poll(conn, router, [], now=5000)
    assert discovery.gone(conn) == []


def test_kolejka_i_karta_pokazuja_usuniete(app, client):
    from .conftest import csrf_from, do_login, enable_2fa, make_user
    with app.state.engine.begin() as c:
        pve = add_integration(c, "proxmox", "pve")
        _poll(c, pve, [("qemu/120", {"name": "stara", "kind": "vm"})], now=1000)
        rid = c.execute(select(external_ref.c.id)).scalar()
        discovery.approve(c, rid)
        did = c.execute(select(external_ref.c.device_id)).scalar()
        _poll(c, pve, [], now=1700)
    make_user(app)
    do_login(client)
    enable_2fa(client, app)
    assert "Nie ma go już w źródle" in client.get(f"/urzadzenia/{did}").text
    page = client.get("/zatwierdz").text
    assert "Usunięte w źródle" in page and f'name="g_{rid}"' in page
    r = client.post("/zatwierdz/wybrane", data={"csrf_token": csrf_from(page), f"g_{rid}": "usun"})
    assert r.status_code == 303
    with app.state.engine.connect() as c:
        assert inv.get_device(c, did) is None
