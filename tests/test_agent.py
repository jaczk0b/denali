"""Agent: parsery (apt, fstab, /proc/mounts, jądra), usługi systemd, rejestracja kodem, raport → stan, aktualizacje,
restart, zależność NFS → TrueNAS, przypięcie do VM, milczący agent."""
import hashlib
import importlib.util
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from denali import agents, discovery, integrations, inventory as inv, updates
from denali.models import dependency, external_ref, integration, observed_state

from .conftest import csrf_from, do_login, enable_2fa, make_user, new_client

AGENT_PATH = Path(__file__).resolve().parent.parent / "denali" / "agent" / "denali-agent.py"
spec = importlib.util.spec_from_file_location("denali_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
REAL_NFS = agent.nfs

APT_LIST = """Listing...
libc6/stable-security 2.36-9+deb12u10 amd64 [upgradable from: 2.36-9+deb12u9]
openssl/stable 3.0.16-1~deb12u1 amd64 [upgradable from: 3.0.15-1~deb12u1]
docker-ce/bookworm 5:28.1.1-1~debian.12~bookworm amd64 [upgradable from: 5:28.0.4-1~debian.12~bookworm]
"""
FSTAB = """# /etc/fstab
UUID=abcd / ext4 errors=remount-ro 0 1
192.168.1.30:/mnt/tank/media /mnt/media nfs defaults,_netdev 0 0
192.168.1.30:/mnt/tank/zdjecia /mnt/zdjecia nfs4 rw,noauto 0 0
truenas.lan:/mnt/tank/kopie /mnt/kopie nfs x-systemd.automount,noauto 0 0
"""
MOUNTS = """/dev/sda1 / ext4 rw,relatime 0 0
192.168.1.30:/mnt/tank/media /mnt/media nfs4 rw,relatime,vers=4.2 0 0
systemd-1 /mnt/kopie autofs rw,relatime 0 0
"""


def test_parsery():
    pkgs = agent.parse_apt(APT_LIST)
    assert [(p["name"], p["old"], p["new"], p["security"]) for p in pkgs] == [
        ("libc6", "2.36-9+deb12u9", "2.36-9+deb12u10", True),
        ("openssl", "3.0.15-1~deb12u1", "3.0.16-1~deb12u1", False),
        ("docker-ce", "5:28.0.4-1~debian.12~bookworm", "5:28.1.1-1~debian.12~bookworm", False)]
    fstab = {e["mount"]: e["noauto"] for e in agent.parse_fstab_nfs(FSTAB)}
    assert fstab == {"/mnt/media": False, "/mnt/zdjecia": True, "/mnt/kopie": False}   # automount to nie „noauto”
    assert max(["6.1.0-9-amd64", "6.1.0-30-amd64", "6.1.0-28-amd64"], key=agent._version_key) == "6.1.0-30-amd64"


def test_nfs(monkeypatch):
    monkeypatch.setattr(agent, "_probe", lambda m: True)
    rows = {r["mount"]: r for r in agent.nfs(MOUNTS, FSTAB, hidden={"/mnt/media": {"entries": 3, "bytes": 10}})}
    assert set(rows) == {"/mnt/media", "/mnt/kopie"}             # /mnt/zdjecia: noauto i niezamontowany — nie pilnujemy
    assert rows["/mnt/media"]["responding"] and rows["/mnt/media"]["hidden"]["entries"] == 3
    assert rows["/mnt/kopie"]["mounted"]                          # automount (autofs) liczy się jako podłączony

    def hang(m):
        raise OSError("Stale file handle")
    monkeypatch.setattr(agent, "_probe", hang)
    rows = {r["mount"]: r for r in agent.nfs(MOUNTS, FSTAB, hidden={})}
    assert rows["/mnt/media"]["responding"] is False and rows["/mnt/media"]["error"] == "error"
    rows = {r["mount"]: r for r in agent.nfs("/dev/sda1 / ext4 rw 0 0\n", FSTAB, hidden={})}
    assert rows["/mnt/media"]["mounted"] is False


def test_uslugi_systemd():
    units = {n: t.format(user=agent.USER, program=agent.PROGRAM) for n, t in agent.UNITS.items()}
    main = units["denali-agent.service"]
    assert "User=denali-agent" in main and "NoNewPrivileges=yes" in main and "ProtectSystem=strict" in main
    hidden = units["denali-agent-ukryte.service"]
    assert "User=" not in hidden and "PrivateNetwork=yes" in hidden       # root, ale bez sieci
    assert "OnUnitActiveSec=1d" in units["denali-agent-ukryte.timer"]


# ───────────────────────── Denali ─────────────────────────
def fake_report(monkeypatch, *, apt_list=APT_LIST, mounts=MOUNTS, fstab=FSTAB, hidden=None,
                kernel_newest="6.1.0-30-amd64", reboot_file=False):
    monkeypatch.setattr(agent, "os_name", lambda: "Debian GNU/Linux 12 (bookworm)")
    monkeypatch.setattr(agent, "virt", lambda: "kvm")
    monkeypatch.setattr(agent, "addresses", lambda: [{"kind": "mac", "value": "bc:24:11:00:00:07"},
                                                     {"kind": "ip", "value": "192.168.1.20"}])
    monkeypatch.setattr(agent, "kernel", lambda: {"running": "6.1.0-28-amd64", "newest": kernel_newest})
    monkeypatch.setattr(agent, "reboot", lambda: {"required": reboot_file, "packages": ["libc6"] if reboot_file else []})
    monkeypatch.setattr(agent, "apt", lambda: {"packages": agent.parse_apt(apt_list), "lists_age_h": 5.0})
    monkeypatch.setattr(agent, "_probe", lambda m: True)
    monkeypatch.setattr(agent, "nfs", lambda: REAL_NFS(mounts, fstab, hidden=hidden or {}))
    rep = agent.collect()
    rep["hostname"] = "docker"
    return rep


@pytest.fixture
def admin(app):
    make_user(app)
    with new_client(app) as c:
        do_login(c)
        enable_2fa(c, app)
        yield c


def enroll_code(admin):
    page = admin.get("/admin/agenci/nowy").text
    r = admin.post("/admin/agenci/nowy", data={"csrf_token": csrf_from(page)})
    return r.text, re.search(r'<strong class="mono">([A-Z0-9-]+)</strong>', r.text).group(1)


def test_polecenie_instalacji_sprawdza_skrot_pliku(app, admin, client):
    page, code = enroll_code(admin)
    body = client.get("/agent/denali-agent.py").content          # plik publiczny w LAN (bez sekretów)
    sha = hashlib.sha256(body).hexdigest()
    assert f'echo "{sha}  denali-agent.py" | sha256sum -c' in page.replace("&#34;", '"')
    assert f"sudo python3 denali-agent.py install {app.state.settings.base_url} {code}" in page
    command = re.search(r"<pre[^>]*>(.*?)</pre>", page, re.S).group(1)
    assert command.startswith("curl -fsSLo denali-agent.py ") and "| sh " not in command and "| sudo" not in command


def test_rejestracja_kodem_jednorazowym(app, admin, client):
    _, code = enroll_code(admin)
    r = client.post("/api/agent/v1/rejestracja", json={"code": code.lower(), "hostname": "docker"})
    assert r.status_code == 200 and r.json()["token"]
    token = r.json()["token"]
    assert client.post("/api/agent/v1/rejestracja", json={"code": code, "hostname": "inna"}).status_code == 403
    with app.state.engine.connect() as c:
        [row] = agents.agent_rows(c)
        assert row["name"] == "agent: docker" and token not in str(row["config"])     # w bazie tylko skrót
    # ta sama maszyna zainstalowana ponownie: nowy token, ta sama integracja
    _, code2 = enroll_code(admin)
    token2 = client.post("/api/agent/v1/rejestracja", json={"code": code2, "hostname": "docker"}).json()["token"]
    with app.state.engine.connect() as c:
        assert [r["id"] for r in agents.agent_rows(c)] == [row["id"]]
        assert agents.authenticate(c, token) is None and agents.authenticate(c, token2)
    # zgadywanie kodu: limit prób z jednego adresu
    for _ in range(30):
        last = client.post("/api/agent/v1/rejestracja", json={"code": "ZLYKOD1234", "hostname": "x"})
    assert last.status_code == 429


def test_raport_stan_aktualizacje_restart_i_nfs(app, admin, client, monkeypatch):
    with app.state.engine.begin() as c:
        vm = inv.create_device(c, "docker", "vm")                    # VM z Proxmoxa (tu: dodana ręcznie)
        inv.add_address(c, vm, "ip", "192.168.1.20")
        nas = inv.create_device(c, "truenas", "nas")
        inv.add_address(c, nas, "ip", "192.168.1.30")
    _, code = enroll_code(admin)
    token = client.post("/api/agent/v1/rejestracja", json={"code": code, "hostname": "docker"}).json()["token"]
    auth_h = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/agent/v1/raport", json=fake_report(monkeypatch), headers=auth_h).status_code == 200
    with app.state.engine.connect() as c:
        ref = c.execute(select(external_ref).where(external_ref.c.external_id == "host")).mappings().one()
        assert ref["device_id"] == vm and ref["status"] == "zatwierdzony"   # przypięty sam, bez kolejki
        iid = ref["integration_id"]
        st = c.execute(select(observed_state).where(observed_state.c.integration_id == iid)).mappings().one()
        assert st["state"] == "ok" and "wymagany restart (nowe jądro 6.1.0-30-amd64)" in st["summary"]
        assert "NFS: 2/2 w porządku" in st["summary"]
        deps = c.execute(select(dependency).where(dependency.c.device_id == vm)).mappings().all()
        assert [(d["depends_on_id"], d["type"], d["hard"], d["source"]) for d in deps] == [(nas, "dane", True, f"dane:{iid}")]
        [row] = updates.overview(c)
        assert row["device"].id == vm and row["reboot"] and row["security"] == 1 and len(row["rows"]) == 3

    # pod montażem leżą stare dane → „uwaga” na urządzeniu
    rep = fake_report(monkeypatch, hidden={"/mnt/media": {"entries": 12, "bytes": 3 * 1024 ** 3, "checked": time.time()}},
                      kernel_newest="6.1.0-28-amd64")
    client.post("/api/agent/v1/raport", json=rep, headers=auth_h)
    with app.state.engine.connect() as c:
        st = c.execute(select(observed_state).where(observed_state.c.integration_id == iid)).mappings().one()
        assert st["state"] == "warn" and "pod montażem leżą stare dane (12 pozycji, 3,0 GB)" in st["summary"]
        assert not st["data"]["reboot_required"]
    page = admin.get(f"/admin/integracje/{iid}").text
    assert "pod montażem: 12 pozycji" in page and "/mnt/media" in page

    # udział w fstab, ale niezamontowany (np. TrueNAS leżał przy starcie) → „uwaga”, zależność zostaje,
    # żeby analiza wskazała TrueNAS
    no_mounts = "/dev/sda1 / ext4 rw 0 0\n"
    client.post("/api/agent/v1/raport", json=fake_report(monkeypatch, mounts=no_mounts), headers=auth_h)
    with app.state.engine.connect() as c:
        st = c.execute(select(observed_state).where(observed_state.c.integration_id == iid)).mappings().one()
        assert st["state"] == "warn" and "/mnt/media niezamontowany" in st["summary"]
        assert c.execute(select(dependency).where(dependency.c.device_id == vm)).first() is not None
    # udział usunięty z fstab → zależność znika przy następnym raporcie
    client.post("/api/agent/v1/raport", json=fake_report(monkeypatch, mounts=no_mounts, fstab=""), headers=auth_h)
    with app.state.engine.connect() as c:
        assert c.execute(select(dependency).where(dependency.c.device_id == vm)).first() is None

    assert client.post("/api/agent/v1/raport", json=rep, headers={"Authorization": "Bearer zly"}).status_code == 401
    assert client.post("/api/agent/v1/raport", content=b"x" * (2 * 1024 * 1024), headers=auth_h).status_code == 400


def test_nowa_maszyna_i_milczacy_agent(app, admin, client, monkeypatch):
    _, code = enroll_code(admin)
    token = client.post("/api/agent/v1/rejestracja", json={"code": code, "hostname": "docker"}).json()["token"]
    client.post("/api/agent/v1/raport", json=fake_report(monkeypatch), headers={"Authorization": f"Bearer {token}"})
    with app.state.engine.begin() as c:
        [d] = c.execute(select(inv.device)).mappings().all()          # nic nie pasowało → nowe urządzenie
        assert d["name"] == "docker" and d["kind"] == "vm"
        [row] = agents.agent_rows(c)
        assert row not in integrations.due(c, now=row["last_ok_at"] + 3600)   # agenta się nie odpytuje
        agents.mark_silent(c, now=row["last_ok_at"] + agents.SILENT_AFTER + 1)
        row = integrations.get(c, row["id"])
        assert row["last_error"].startswith("Agent nie wysłał raportu od")
        st = c.execute(select(observed_state).where(observed_state.c.integration_id == row["id"])).mappings().one()
        assert st["state"] == "unknown"
    # strona agenta zamiast formularza; zapis formularza nie skasuje skrótu tokenu
    page = admin.get(f"/admin/integracje/{row['id']}").text
    assert "Usuń agenta" in page and "Odczytaj teraz" not in page
    r = admin.post(f"/admin/integracje/{row['id']}", data={"csrf_token": csrf_from(page), "name": "x"})
    assert r.status_code == 404
    admin.post(f"/admin/agenci/{row['id']}/usun", data={"csrf_token": csrf_from(page)})
    assert client.post("/api/agent/v1/raport", json={}, headers={"Authorization": f"Bearer {token}"}).status_code == 401
