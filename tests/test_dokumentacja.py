import pytest
from sqlalchemy import select

from denali import docs, files, inventory as inv
from denali.models import procedure_step


def test_runbook_dla_wielu_urzadzen(conn):
    a = inv.create_device(conn, "pve", "host")
    b = inv.create_device(conn, "pbs", "vm", parent_id=a)
    rid = docs.save_runbook(conn, "Restart klastra", "1. …", [a, b], user="jakub")
    assert [r["id"] for r in docs.runbooks_for_device(conn, b)] == [rid]
    docs.save_runbook(conn, "Restart klastra", "1. …", [a], runbook_id=rid)
    assert docs.runbooks_for_device(conn, b) == []


def test_procedura_z_krokami_kontaktami_i_urzadzeniami(conn):
    pve = inv.create_device(conn, "pve", "host")
    c = docs.save_contact(conn, "Elektryk", "+48 600 100 200")
    pid = docs.save_procedure(conn, "Padł pve", [("Sprawdź zasilanie", "UPS w szafie"), ("Uruchom ręcznie", "")],
                              device_ids=[pve], contact_ids=[c], user="jakub")
    p = docs.get_procedure(conn, pid)
    assert [s["title"] for s in p["steps"]] == ["Sprawdź zasilanie", "Uruchom ręcznie"]
    assert [s["position"] for s in p["steps"]] == [1, 2]
    assert [d["name"] for d in p["devices"]] == ["pve"] and p["contacts"][0]["phone"] == "+48 600 100 200"
    docs.save_procedure(conn, "Padł pve", [("Jeden krok", "")], procedure_id=pid)
    assert conn.execute(select(procedure_step.c.title)).scalars().all() == ["Jeden krok"]


def test_procedura_pokazuje_sie_przy_awarii_po_wyzwalaczu(conn):
    pve = inv.create_device(conn, "pve", "host")
    ap = inv.create_device(conn, "AP salon", "ap")
    p1 = docs.save_procedure(conn, "Padł pve", [("Krok", "")], trigger="device_down:PVE")
    p2 = docs.save_procedure(conn, "Padł AP", [("Krok", "")], trigger="device_down:typ:ap")
    docs.save_procedure(conn, "Inna", [("Krok", "")])
    assert [p["id"] for p in docs.procedures_for_device(conn, pve)] == [p1]
    assert [p["id"] for p in docs.procedures_for_device(conn, ap)] == [p2]


@pytest.mark.parametrize("kwargs, msg", [
    ({"title": "", "steps": [("a", "")]}, "tytuł"),
    ({"title": "x", "steps": []}, "co najmniej jednego"),
    ({"title": "x", "steps": [("a", "")], "trigger": "rm -rf /"}, "Wyzwalacz"),
    ({"title": "x", "steps": [("a", "")], "scope": "sklep"}, "infrastruktury albo panelu"),
    ({"title": "x", "steps": [("a", "")], "device_ids": [999]}, "nie istnieje"),
])
def test_walidacja_procedur(conn, kwargs, msg):
    with pytest.raises(docs.DocsError, match=msg):
        docs.save_procedure(conn, **kwargs)


def test_zly_telefon_kontaktu(conn):
    with pytest.raises(docs.DocsError):
        docs.save_contact(conn, "X", "<script>")


# ───────────────────────── pliki ─────────────────────────
def test_plik_pod_losowa_nazwa(conn, settings):
    d = inv.create_device(conn, "pve", "host")
    fid = files.store(conn, settings.data_dir, d, "..\\..\\etc/passwd", b"dane", "text/html; charset=utf-8")
    row, path = files.get(conn, settings.data_dir, fid)
    assert row["name"] == "passwd" and row["mime"] == "text/html" and row["size"] == 4
    assert path.parent == files.files_dir(settings.data_dir) and path.read_bytes() == b"dane"
    files.remove(conn, settings.data_dir, fid)
    assert not path.exists() and files.get(conn, settings.data_dir, fid) is None


def test_limity_plikow(conn, settings, monkeypatch):
    d = inv.create_device(conn, "pve", "host")
    with pytest.raises(files.FileError, match="pusty"):
        files.store(conn, settings.data_dir, d, "a.txt", b"")
    monkeypatch.setattr(files, "MAX_SIZE", 10)
    with pytest.raises(files.FileError, match="za duży"):
        files.store(conn, settings.data_dir, d, "a.txt", b"x" * 11)
    fid = files.store(conn, settings.data_dir, d, "a.txt", b"x", "nie/poprawny typ")
    assert files.get(conn, settings.data_dir, fid)[0]["mime"] == "application/octet-stream"


def test_sprzatanie_plikow_po_usunieciu_urzadzenia(conn, settings):
    d = inv.create_device(conn, "pve", "host")
    keep = inv.create_device(conn, "nas", "nas")
    files.store(conn, settings.data_dir, d, "a.jpg", b"1")
    files.store(conn, settings.data_dir, keep, "b.jpg", b"2")
    inv.delete_device(conn, d)
    assert files.cleanup_orphans(conn, settings.data_dir) == 1
    assert len(list(files.files_dir(settings.data_dir).iterdir())) == 1
