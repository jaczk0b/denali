import pytest
from sqlalchemy import select

from denali import inventory as inv
from denali.models import device, device_address


def test_urzadzenie_wymaga_tylko_nazwy_i_typu(conn):
    did = inv.create_device(conn, "  pve  ", "host")
    d = inv.get_device(conn, did)
    assert d["name"] == "pve" and d["criticality"] == 3 and d["pulpit_group"] == "brak"


@pytest.mark.parametrize("fields, msg", [
    ({"name": "", "kind": "host"}, "Podaj nazwę"),
    ({"name": "x", "kind": "kasa"}, "Nieznany typ"),
    ({"name": "x", "kind": "host", "criticality": 7}, "P1, P2 albo P3"),
    ({"name": "x", "kind": "usluga", "app_url": "javascript:alert(1)"}, "http://"),
    ({"name": "x", "kind": "host", "owner": "ja"}, "Nieznane pole"),
])
def test_walidacja(conn, fields, msg):
    with pytest.raises(inv.InventoryError, match=msg):
        inv.create_device(conn, **fields)


def test_reguly_na_czym_dziala(conn):
    pve = inv.create_device(conn, "pve", "host")
    vm = inv.create_device(conn, "docker", "vm", parent_id=pve)
    inv.create_device(conn, "immich", "kontener", parent_id=vm)
    router = inv.create_device(conn, "router", "router")
    with pytest.raises(inv.InventoryError, match="nie może działać"):
        inv.create_device(conn, "zly", "kontener", parent_id=router)
    with pytest.raises(inv.InventoryError, match="nie działa na innym"):
        inv.create_device(conn, "host2", "host", parent_id=pve)
    with pytest.raises(inv.InventoryError, match="nie może działać"):
        inv.update_device(conn, vm, kind="lxc", parent_id=vm)


def test_zmiana_typu_musi_pasowac_do_rodzica(conn):
    pve = inv.create_device(conn, "pve", "host")
    vm = inv.create_device(conn, "docker", "vm", parent_id=pve)
    with pytest.raises(inv.InventoryError):
        inv.update_device(conn, vm, kind="router")


def test_petla_przez_zaleznosc_i_kabel_zablokowana(conn):
    pve = inv.create_device(conn, "pve", "host")
    vm = inv.create_device(conn, "truenas", "vm", parent_id=pve)
    with pytest.raises(inv.InventoryError, match="pętlę"):
        inv.add_dependency(conn, pve, vm, "dane")
    switch = inv.create_device(conn, "switch", "switch")
    inv.add_cable(conn, switch, pve, "port 3", "nic0")
    with pytest.raises(inv.InventoryError, match="pętlę"):
        inv.add_cable(conn, vm, switch)


def test_ta_sama_zaleznosc_dwa_razy_to_jeden_wpis(conn):
    a = inv.create_device(conn, "immich", "usluga")
    b = inv.create_device(conn, "truenas", "nas")
    assert inv.add_dependency(conn, a, b, "dane") == inv.add_dependency(conn, a, b, "dane")
    with pytest.raises(inv.InventoryError, match="rodzaj"):
        inv.add_dependency(conn, a, b, "hosting")


def test_adresy_normalizowane(conn):
    d = inv.create_device(conn, "pve", "host")
    inv.add_address(conn, d, "mac", "AA-BB-CC-DD-EE-0F")
    inv.add_address(conn, d, "ip", " 192.168.1.10 ", primary=True)
    inv.add_address(conn, d, "ip", "192.168.1.10")             # bez duplikatu
    rows = conn.execute(select(device_address.c.kind, device_address.c.value)
                        .where(device_address.c.device_id == d)).all()
    assert sorted(rows) == [("ip", "192.168.1.10"), ("mac", "aa:bb:cc:dd:ee:0f")]
    assert inv.find_by_address(conn, "mac", "aa:bb:cc:dd:ee:0f") == d
    with pytest.raises(inv.InventoryError, match="adres IP"):
        inv.add_address(conn, d, "ip", "192.168.1.300")


def test_usuniecie_urzadzenia(conn):
    pve = inv.create_device(conn, "pve", "host")
    vm = inv.create_device(conn, "docker", "vm", parent_id=pve)
    inv.add_address(conn, pve, "ip", "192.168.1.10")
    inv.delete_device(conn, pve)
    assert inv.get_device(conn, vm)["parent_id"] is None       # VM zostaje, traci tylko „na czym działa”
    assert conn.execute(select(device_address)).first() is None


def test_graf_z_bazy_wskazuje_przyczyne(conn):
    router = inv.create_device(conn, "router", "router")
    switch = inv.create_device(conn, "switch", "switch")
    pve = inv.create_device(conn, "pve", "host")
    truenas = inv.create_device(conn, "truenas", "vm", parent_id=pve)
    docker = inv.create_device(conn, "docker", "vm", parent_id=pve)
    immich = inv.create_device(conn, "immich", "kontener", parent_id=docker)
    inv.add_cable(conn, router, switch)
    inv.add_cable(conn, switch, pve)
    inv.add_dependency(conn, immich, truenas, "dane")
    g = inv.load_graph(conn)
    ids = {r.name: r.id for r in conn.execute(select(device.c.id, device.c.name))}
    states = {i: "ok" for i in ids.values()} | {truenas: "down", immich: "down"}
    [f] = g.analyze(states)
    assert g.name(f.cause) == "truenas" and f.effects == [immich]
    [f] = g.analyze({i: "down" for i in ids.values()} | {router: "ok"})
    assert g.name(f.cause) == "switch"
