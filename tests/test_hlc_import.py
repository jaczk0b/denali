"""Migracja z HLC na kopii w formacie `python -m hlc backup` (ZIP: instance/hlc.db + instance/uploads/)."""
import hashlib
import json
import sqlite3
import zipfile

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from denali import docs, hlc_import, inventory as inv, vault
from denali.models import contact, cable, dependency, device, device_address, incident, runbook, vault_secret

OLD_KEY = Fernet.generate_key().decode()
SCHEMA = """
CREATE TABLE sites (id TEXT PRIMARY KEY, name TEXT NOT NULL, call TEXT DEFAULT '', panic TEXT DEFAULT '[]',
  subnets TEXT DEFAULT '[]', wifi TEXT DEFAULT '[]', sort INTEGER DEFAULT 0);
CREATE TABLE devices (id TEXT PRIMARY KEY, site_id TEXT, name TEXT, kind TEXT, ip TEXT DEFAULT '', crit INTEGER DEFAULT 1,
  parent_id TEXT DEFAULT '', purpose TEXT DEFAULT '', access TEXT DEFAULT '', vault TEXT DEFAULT '', backup TEXT DEFAULT '',
  restore_tested TEXT DEFAULT '', place TEXT DEFAULT '', serial TEXT DEFAULT '', runbook TEXT DEFAULT '', notes TEXT DEFAULT '',
  cost TEXT, ports TEXT DEFAULT '[]', expiry TEXT DEFAULT '[]', panel TEXT DEFAULT '[]', checked TEXT DEFAULT '',
  approved INTEGER DEFAULT 1, pve_type TEXT DEFAULT '');
CREATE TABLE deps (id TEXT PRIMARY KEY, device_id TEXT, parent_id TEXT, type TEXT DEFAULT 'siec', hard INTEGER DEFAULT 1, note TEXT DEFAULT '');
CREATE TABLE links (id TEXT PRIMARY KEY, site_id TEXT, a TEXT, ap TEXT DEFAULT '', b TEXT, bp TEXT DEFAULT '');
CREATE TABLE issues (id TEXT PRIMARY KEY, device_id TEXT, date TEXT, title TEXT, "desc" TEXT DEFAULT '', status TEXT DEFAULT 'open',
  fix TEXT DEFAULT '', closed TEXT DEFAULT '', created TEXT);
CREATE TABLE files (id TEXT PRIMARY KEY, device_id TEXT, name TEXT, mime TEXT, size INTEGER, stored TEXT, uploaded TEXT);
CREATE TABLE secrets (id TEXT PRIMARY KEY, device_id TEXT, label TEXT, "desc" TEXT DEFAULT '', scheme TEXT DEFAULT 'fernet', blob_enc TEXT);
CREATE TABLE apps (id TEXT PRIMARY KEY, name TEXT, url TEXT, icon TEXT DEFAULT '', description TEXT DEFAULT '',
  category TEXT DEFAULT '', device_id TEXT DEFAULT '', audience TEXT DEFAULT 'all', sort INTEGER DEFAULT 0);
CREATE TABLE net_hosts (mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT DEFAULT '', device_id TEXT DEFAULT '');
CREATE TABLE dns_names (name TEXT, device_id TEXT, PRIMARY KEY (name, device_id));
CREATE TABLE checks (id TEXT PRIMARY KEY, device_id TEXT);
CREATE TABLE users (id TEXT PRIMARY KEY, login TEXT);
"""


def make_hlc(tmp_path, key=OLD_KEY):
    f = Fernet(key)
    inst = tmp_path / "instance"
    (inst / "uploads").mkdir(parents=True)
    db = sqlite3.connect(inst / "hlc.db")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO sites VALUES ('s1','Dom','Elektryk Jan 600 100 200',?, '[]', ?, 0)", (
        json.dumps([{"t": "Nie ma internetu", "steps": ["Router z prądu na 30 s.", "Po 5 min lampka WAN; potem dostawca."]}]),
        json.dumps([{"ssid": "DomWiFi", "pass": {"hlc_enc": f.encrypt(b"wifi-haslo-123").decode()}, "pasmo": "5 GHz"}])))
    db.execute("INSERT INTO sites VALUES ('s2','Sklep','', '[]', '[]', '[]', 1)")
    devs = [
        ("router", "s1", "Router", "router", "192.168.1.1", 3, "", "", ""),
        ("switch", "s1", "Switch", "switch", "", 2, "", "", ""),
        ("pve", "s1", "pve", "host", "192.168.1.10", 3, "", "", ""),
        ("truenas", "s1", "TrueNAS", "vm", "192.168.1.30:443", 3, "pve", "qemu", "dane: zdjęcia"),
        ("docker", "s1", "docker", "vm", "zły adres!", 2, "pve", "qemu", ""),
        ("immich", "s1", "Immich", "kontener", "", 2, "docker", "", "zdjęcia"),
        ("kasa", "s2", "Kasa 1", "kasa", "192.168.0.20", 2, "", "", ""),
    ]
    for d in devs:
        db.execute("INSERT INTO devices (id, site_id, name, kind, ip, crit, parent_id, pve_type, purpose) VALUES (?,?,?,?,?,?,?,?,?)", d)
    db.execute("UPDATE devices SET runbook=?, notes=?, access=?, ports=? WHERE id='immich'", (
        "# Restart Immich\n\n1. docker compose restart", "admin hasło: tajne123", "https://immich.lan",
        json.dumps([{"port": 2283, "opis": "web"}])))
    db.execute("INSERT INTO devices (id, site_id, name, kind, approved) VALUES ('nowy','s1','?? nowy z Proxmoxa','vm',0)")
    db.execute("INSERT INTO deps VALUES ('d1','immich','truenas','dane',1,'NFS')")
    db.execute("INSERT INTO links VALUES ('l1','s1','router','LAN1','switch','1')")
    db.execute("INSERT INTO links VALUES ('l2','s1','pve','nic0','switch','3')")      # strona odwrotnie niż w Denali
    db.execute("INSERT INTO issues VALUES ('i1','truenas','2026-03-01','Zawieszony NAS','kernel panic','closed','reset VM','2026-03-01','')")
    db.execute("INSERT INTO issues VALUES ('i2','immich','2026-10-01','Wolne miniatury','','open','','','')")
    db.execute("INSERT INTO files VALUES ('f1','pve','faktura.pdf','application/pdf',9,'abc_faktura.pdf','')")
    (inst / "uploads" / "abc_faktura.pdf").write_bytes(b"%PDF-test")
    db.execute("INSERT INTO files VALUES ('f2','pve','zgubiony.txt','text/plain',1,'nie_ma.txt','')")
    db.execute("INSERT INTO secrets VALUES ('x1','truenas','root','konto root',  'fernet', ?)", (f.encrypt(b"root-haslo").decode(),))
    db.execute("INSERT INTO secrets VALUES ('x2','pve','stare','', 'legacy-vault', 'xxx')")
    db.execute("INSERT INTO apps VALUES ('a1','Zdjęcia','https://photos.lan','📷','','Rodzina','immich','all',0)")
    db.execute("INSERT INTO apps VALUES ('a2','Przepisy','https://mealie.lan','','Mealie','Rodzina','','all',1)")
    db.execute("INSERT INTO apps VALUES ('a3','Proxmox','https://pve.lan:8006','PX','','Admin','pve','admin',2)")
    db.execute("INSERT INTO net_hosts VALUES ('AA:BB:CC:00:00:10','192.168.1.10','pve','pve')")
    db.execute("INSERT INTO dns_names VALUES ('photos.serwispowers.com','immich')")
    db.execute("INSERT INTO checks VALUES ('c1','immich')")
    db.execute("INSERT INTO users VALUES ('u1','admin')")
    db.commit()
    db.close()
    archive = tmp_path / "hlc-2026-10-30.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(inst / "hlc.db", "instance/hlc.db")
        z.write(inst / "uploads" / "abc_faktura.pdf", "instance/uploads/abc_faktura.pdf")
        z.writestr("ODTWARZANIE.txt", "…")
    return archive


def migrate(app, settings, archive, **kw):
    kw.setdefault("old_key", OLD_KEY)
    return hlc_import.migrate(app.state.engine, settings, app.state.box, str(archive), **kw)


def count(app, table):
    with app.state.engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar()


def by_name(c, name):
    return c.execute(select(device).where(device.c.name == name)).mappings().first()


def test_proba_na_sucho_niczego_nie_zapisuje(app, settings, tmp_path):
    archive = make_hlc(tmp_path)
    r = migrate(app, settings, archive)
    assert r.counts["urządzenia"] == 8         # 7 z obu lokalizacji (bez niezatwierdzonego) + aplikacja „Przepisy”
    assert count(app, device) == 0 and count(app, vault_secret) == 0
    assert not (settings.data_dir / "files").exists()
    text = r.text(applied=False)
    assert "PRÓBA NA SUCHO" in text and "--wykonaj" in text
    assert "root-haslo" not in text and "wifi-haslo-123" not in text


def test_pelna_migracja_jednej_lokalizacji(app, settings, tmp_path):
    archive = make_hlc(tmp_path)
    r = migrate(app, settings, archive, sites=["dom"], apply=True)
    with app.state.engine.connect() as c:
        names = set(c.execute(select(device.c.name)).scalars())
        assert "Kasa 1" not in names and "?? nowy z Proxmoxa" not in names       # sklep i niezatwierdzone pominięte
        pve, truenas, immich, docker = (by_name(c, n) for n in ("pve", "TrueNAS", "Immich", "docker"))
        assert pve["criticality"] == 1 and docker["criticality"] == 2           # crit 3 w HLC = P1
        assert truenas["parent_id"] == pve["id"] and immich["parent_id"] == docker["id"]
        assert "## Dostęp" in immich["notes_md"] and "port: 2283" in immich["notes_md"]
        assert immich["app_url"] == "https://photos.lan" and immich["icon"] == "📷" and immich["pulpit_group"] == "codzien"
        assert pve["pulpit_group"] == "zarzadzanie"
        assert by_name(c, "Przepisy")["app_url"] == "https://mealie.lan"
        addrs = {(a.kind, a.value) for a in c.execute(select(device_address))}
        assert ("ip", "192.168.1.30") in addrs and ("mac", "aa:bb:cc:00:00:10") in addrs
        assert ("hostname", "photos.serwispowers.com") in addrs
        assert c.execute(select(dependency.c.type)).scalars().all() == ["dane"]
        assert c.execute(select(func.count()).select_from(cable)).scalar() == 2
        assert c.execute(select(runbook.c.title)).scalars().all() == ["Restart Immich"]
        inc = {i.title: i.status for i in c.execute(select(incident))}
        assert inc == {"Zawieszony NAS": "zamkniety", "Wolne miniatury": "trwa"}
        [p] = docs.list_procedures(c)
        proc = docs.get_procedure(c, p["id"])
        assert [s["title"] for s in proc["steps"]][0] == "Router z prądu na 30 s."
        assert proc["contacts"][0]["phone"] == "600 100 200"
        secrets = {s["label"]: s["id"] for s in vault.list_secrets(c, role="admin")}
        assert set(secrets) == {"root", "Wi-Fi: DomWiFi"}
        assert vault.reveal(c, app.state.box, secrets["root"], role="admin", reauth_at=1.0, user="t", now=2.0) == "root-haslo"
        assert vault.reveal(c, app.state.box, secrets["Wi-Fi: DomWiFi"], role="admin", reauth_at=1.0, user="t",
                            now=2.0) == "wifi-haslo-123"
    stored = list((settings.data_dir / "files").iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == b"%PDF-test"
    warnings = " | ".join(r.warnings)
    assert "zły adres!" in warnings                         # nieprawidłowy adres zgłoszony do przejrzenia
    assert "Immich: notatki albo runbook" in warnings      # ślad hasła w notatkach
    assert "starym skarbcu" in warnings and "zgubiony.txt" in warnings
    assert r.counts["nie przenoszone — konta użytkowników (załóż w Denali: create-user)"] == 1


def test_druga_migracja_niczego_nie_dubluje(app, settings, tmp_path):
    archive = make_hlc(tmp_path)
    migrate(app, settings, archive, sites=["Dom"], apply=True)
    before = {t.name: count(app, t) for t in (device, device_address, dependency, cable, runbook, incident, vault_secret, contact)}
    migrate(app, settings, archive, sites=["Dom"], apply=True)
    assert {t.name: count(app, t) for t in (device, device_address, dependency, cable, runbook, incident, vault_secret,
                                             contact)} == before
    assert len(list((settings.data_dir / "files").iterdir())) == 1


def test_zly_klucz_nie_blokuje_reszty(app, settings, tmp_path):
    archive = make_hlc(tmp_path)
    r = migrate(app, settings, archive, sites=["Dom"], old_key=Fernet.generate_key().decode(), apply=True)
    assert r.counts["hasła NIEodszyfrowane (zły albo brak klucza)"] == 2
    assert count(app, vault_secret) == 0 and count(app, device) > 0


def test_bez_klucza_ostrzezenie(app, settings, tmp_path):
    r = migrate(app, settings, make_hlc(tmp_path), sites=["Dom"], old_key=None)
    assert any("Bez HLC_FERNET_KEY" in w for w in r.warnings)


def test_zla_lokalizacja_i_zly_plik(app, settings, tmp_path):
    archive = make_hlc(tmp_path)
    with pytest.raises(hlc_import.MigrationError, match="Są: Dom, Sklep"):
        migrate(app, settings, archive, sites=["Biuro"])
    bad = tmp_path / "zly.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../../etc/passwd", "x")
        z.writestr("instance/uploads/../../uciekam.txt", "x")
    with pytest.raises(hlc_import.MigrationError, match="nie ma instance/hlc.db"):
        migrate(app, settings, bad)
    assert not (tmp_path.parent / "uciekam.txt").exists()
    with pytest.raises(hlc_import.MigrationError, match="zły format"):
        migrate(app, settings, archive, old_key="Khuba123")


def test_baza_hlc_tylko_do_odczytu(app, settings, tmp_path):
    make_hlc(tmp_path)
    db = tmp_path / "instance" / "hlc.db"
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    migrate(app, settings, tmp_path / "instance", sites=["Dom"], apply=True)    # katalog zamiast ZIP-a
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
    assert count(app, device) > 0


def test_po_migracji_analiza_przyczyny_dziala(app, settings, tmp_path):
    migrate(app, settings, make_hlc(tmp_path), sites=["Dom"], apply=True)
    with app.state.engine.connect() as c:
        g = inv.load_graph(c)
        ids = {r.name: r.id for r in c.execute(select(device.c.id, device.c.name))}
    states = {i: "ok" for i in ids.values()} | {ids["TrueNAS"]: "down", ids["Immich"]: "down"}
    [f] = g.analyze(states)
    assert g.name(f.cause) == "TrueNAS" and [g.name(x) for x in f.effects] == ["Immich"]


def test_kopia_pobrana_przez_http(app, settings, tmp_path):
    """Tak przenosimy dane na serwerze: w konsoli HLC `python3 -m http.server`, w Denali adres http://…"""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    archive = make_hlc(tmp_path)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = migrate(app, settings, f"http://127.0.0.1:{srv.server_port}/{archive.name}", sites=["Dom"], apply=True)
    finally:
        srv.shutdown()
    assert r.counts["urządzenia"] == 7 and r.counts["pliki"] == 1
