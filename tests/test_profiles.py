import pytest
from sqlalchemy import select

from denali import inventory as inv
from denali import profiles
from denali.models import device_override, profile


def pid(conn, name):
    return conn.execute(select(profile.c.id).where(profile.c.name == name)).scalar_one()


def test_profil_po_typie_bez_ustawiania_niczego(conn):
    vm = inv.create_device(conn, "docker", "vm")
    router = inv.create_device(conn, "router", "router")
    ups = inv.create_device(conn, "ups", "ups")
    assert profiles.effective(conn, vm)[0] == "Maszyna wirtualna"
    name, eff = profiles.effective(conn, router)
    assert name == "Sieć" and eff["down_after_s"] == {"value": 60, "source": "profil", "profile_value": 60}
    name, eff = profiles.effective(conn, ups)
    assert name == "Domyślny" and eff["down_after_s"]["source"] == "domyslne"
    assert eff["actions"]["value"] == []             # domyślnie żadnych akcji


def test_wyjatek_na_urzadzeniu_widoczny_jako_inny_niz_profil(conn):
    vm = inv.create_device(conn, "truenas", "vm")
    profiles.set_override(conn, vm, "notify", False)
    eff = profiles.effective(conn, vm)[1]["notify"]
    assert eff == {"value": False, "source": "urzadzenie", "profile_value": True}


def test_wyjatek_rowny_profilowi_znika(conn):
    vm = inv.create_device(conn, "truenas", "vm")
    profiles.set_override(conn, vm, "notify", False)
    profiles.set_override(conn, vm, "notify", True)
    assert conn.execute(select(device_override)).first() is None


def test_zmiana_profilu_dotyczy_wszystkich_poza_wyjatkami(conn):
    a = inv.create_device(conn, "a", "vm")
    b = inv.create_device(conn, "b", "lxc")
    c = inv.create_device(conn, "c", "vm")
    profiles.set_override(conn, c, "down_after_s", 600)
    profiles.set_profile_setting(conn, pid(conn, "Maszyna wirtualna"), "down_after_s", 300)
    assert profiles.value(conn, a, "down_after_s") == 300
    assert profiles.value(conn, b, "down_after_s") == 300
    assert profiles.value(conn, c, "down_after_s") == 600
    profiles.set_profile_setting(conn, pid(conn, "Maszyna wirtualna"), "down_after_s", 120)   # = domyślne
    assert conn.execute(select(profile.c.settings).where(profile.c.name == "Maszyna wirtualna")).scalar_one() == {}


def test_profil_wybrany_recznie_ma_pierwszenstwo(conn):
    vm = inv.create_device(conn, "krytyczna", "vm", profile_id=pid(conn, "Sieć"))
    assert profiles.effective(conn, vm)[0] == "Sieć"


@pytest.mark.parametrize("key, val", [("down_after_s", 5), ("down_after_s", True), ("notify", "tak"),
                                      ("updates", "instaluj"), ("actions", "restart"), ("nieznane", 1)])
def test_zle_wartosci(conn, key, val):
    vm = inv.create_device(conn, "x", "vm")
    with pytest.raises(profiles.ProfileError):
        profiles.set_override(conn, vm, key, val)
