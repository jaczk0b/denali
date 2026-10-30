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
