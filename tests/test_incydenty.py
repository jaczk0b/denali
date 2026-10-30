"""Incydenty: wpisy ręczne i automatyczne z analizy przyczyny (scenariusz TrueNAS z makiety)."""
import pytest
from sqlalchemy import select

from denali import incidents, inventory as inv
from denali.models import incident


def homelab(conn):
    ids = {}
    ids["pve"] = inv.create_device(conn, "pve", "host", criticality=1)
    ids["truenas"] = inv.create_device(conn, "truenas", "vm", parent_id=ids["pve"], criticality=1)
    ids["docker"] = inv.create_device(conn, "docker", "vm", parent_id=ids["pve"])
    for app in ("immich", "jellyfin"):
        ids[app] = inv.create_device(conn, app, "kontener", parent_id=ids["docker"])
        inv.add_dependency(conn, ids[app], ids["truenas"], "dane")
    ids["pbs"] = inv.create_device(conn, "pbs", "vm", parent_id=ids["pve"])
    inv.add_dependency(conn, ids["pbs"], ids["truenas"], "dane")
    ids["ha"] = inv.create_device(conn, "homeassistant", "vm", parent_id=ids["pve"])
    inv.add_dependency(conn, ids["ha"], ids["pbs"], "dane", hard=False)
    return ids


def analyze(conn, ids, down):
    g = inv.load_graph(conn)
    states = {i: "ok" for i in ids.values()} | {ids[n]: "down" for n in down}
    return g, g.analyze(states)


def test_awaria_truenas_to_jeden_incydent_z_dowodami(conn):
    ids = homelab(conn)
    g, [f] = analyze(conn, ids, ["truenas", "immich", "pbs"])
    iid, new = incidents.record_finding(conn, g, f, now=1000)
    inc = incidents.get_incident(conn, iid)
    assert new and inc["title"] == "truenas nie działa" and inc["severity"] == "krytyczny" and inc["source"] == "auto"
    assert [(d["name"], d["role"]) for d in inc["devices"]] == [
        ("truenas", "przyczyna"), ("immich", "skutek"), ("pbs", "skutek"), ("homeassistant", "gorzej")]
    texts = [e["text"] for e in inc["events"]]
    assert "pve działa (działa na nim), więc przyczyna nie jest wyżej." in texts
    assert "Przez to nie działa: immich, pbs." in texts

    # minutę później pada jeszcze jellyfin — ten sam incydent, nowy wpis w przebiegu
    g, [f] = analyze(conn, ids, ["truenas", "immich", "pbs", "jellyfin"])
    iid2, new = incidents.record_finding(conn, g, f, now=1060)
    assert iid2 == iid and not new
    assert incidents.get_incident(conn, iid)["events"][-1]["text"] == "Przestało też działać: jellyfin."

    assert incidents.recover(conn, ids["truenas"], now=1500) == iid
    inc = incidents.get_incident(conn, iid)
    assert inc["status"] == "zamkniety" and inc["ended_at"] == 1500


def test_po_zamknieciu_nowa_awaria_to_nowy_incydent(conn):
    ids = homelab(conn)
    g, [f] = analyze(conn, ids, ["truenas"])
    iid, _ = incidents.record_finding(conn, g, f)
    incidents.recover(conn, ids["truenas"])
    iid2, new = incidents.record_finding(conn, g, f)
    assert new and iid2 != iid


def test_waga_z_krytycznosci(conn):
    ids = homelab(conn)
    g, [f] = analyze(conn, ids, ["docker", "immich", "jellyfin"])
    iid, _ = incidents.record_finding(conn, g, f)
    assert incidents.get_incident(conn, iid)["severity"] == "ostrzezenie"


def test_wpis_reczny_pelny_cykl(conn):
    ids = homelab(conn)
    iid = incidents.open_manual(conn, "Wolny Jellyfin wieczorem", "jakub", device_id=ids["jellyfin"],
                                severity="info", now=100)
    incidents.ack(conn, iid, "jakub", now=110)
    incidents.add_note(conn, iid, "Transkodowanie na CPU", "jakub", now=120)
    incidents.close(conn, iid, "jakub", cause_md="Brak akceleracji GPU", fix_md="Włączone QSV", now=130)
    inc = incidents.get_incident(conn, iid)
    assert inc["status"] == "zamkniety" and inc["acked_by"] == "jakub" and inc["fix_md"] == "Włączone QSV"
    assert [e["kind"] for e in inc["events"]] == ["otwarty", "potwierdzony", "notatka", "zamkniety"]
    incidents.reopen(conn, iid, "jakub")
    assert incidents.get_incident(conn, iid)["status"] == "trwa"


def test_historia_urzadzenia_i_otwarte(conn):
    ids = homelab(conn)
    g, [f] = analyze(conn, ids, ["truenas", "immich"])
    auto, _ = incidents.record_finding(conn, g, f, now=200)
    manual = incidents.open_manual(conn, "Wymiana dysku", "jakub", device_id=ids["pve"], now=100)
    incidents.close(conn, manual, "jakub")
    assert [i["id"] for i in incidents.list_incidents(conn, device_id=ids["immich"])] == [auto]
    assert [i["id"] for i in incidents.list_incidents(conn, open_only=True)] == [auto]
    assert [i["id"] for i in incidents.list_incidents(conn)] == [auto, manual]


def test_walidacja(conn):
    with pytest.raises(incidents.IncidentError):
        incidents.open_manual(conn, "  ", "jakub")
    with pytest.raises(incidents.IncidentError):
        incidents.add_note(conn, 999, "x", "jakub")
    assert conn.execute(select(incident)).first() is None
