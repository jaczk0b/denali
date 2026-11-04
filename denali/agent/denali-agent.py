#!/usr/bin/env python3
"""Agent Denali — tylko to, czego nie da się odczytać z API: pakiety apt, „wymagany restart”, montaże NFS.

Jeden plik, tylko biblioteka standardowa (Python 3.9+, Debian/Ubuntu z systemd). Co minutę wysyła raport do Denali.
Nie słucha na żadnym porcie. Akcję zleconą przez panel (np. apt upgrade) wykona tylko wtedy, gdy jest na lokalnej
liście dozwolonych akcji w /etc/denali-agent/config.json (plik roota — panel go nie zmieni). Domyślnie lista jest pusta.

    sudo python3 denali-agent.py install https://denali.example.com KOD-Z-PANELU [--akcje apt_upgrade,reboot]
    sudo python3 /opt/denali-agent/denali-agent.py pozwol apt_upgrade     # dopisz akcję do lokalnej listy
    sudo python3 /opt/denali-agent/denali-agent.py pozwol reboot          # restart maszyny z panelu
    sudo python3 /opt/denali-agent/denali-agent.py pozwol polecenia       # gotowe polecenia z karty Terminale
    sudo python3 /opt/denali-agent/denali-agent.py zabron apt_upgrade     # usuń akcję z listy
    sudo python3 /opt/denali-agent/denali-agent.py uninstall
    python3 /opt/denali-agent/denali-agent.py print        # raport na ekran, bez wysyłania

Usługi po instalacji:
  denali-agent.service        raport co minutę; zwykły użytkownik systemowy denali-agent, system tylko do odczytu
                              (poza katalogami zleceń i wyników); zlecenia z panelu tylko zapisuje do pliku
  denali-agent-akcje.path     gdy pojawi się zlecenie, uruchamia denali-agent-akcje.service (root): sprawdza lokalną
                              listę dozwolonych, wykonuje akcję, zapisuje wynik — raport zaniesie go do Denali
  denali-agent-ukryte.timer   raz dziennie, jako root, BEZ dostępu do sieci: czy pod montażami NFS nie leżą dane
                              zapisane wtedy, gdy udział nie był podłączony (wymaga bind-mountu, stąd root)
"""
import argparse
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.3.0"
USER = "denali-agent"
CONFIG_PATH = "/etc/denali-agent/config.json"
INSTALL_DIR = "/opt/denali-agent"
PROGRAM = INSTALL_DIR + "/denali-agent.py"
STATE_DIR = "/var/lib/denali-agent"
HIDDEN_FILE = STATE_DIR + "/ukryte.json"
JOBS_DIR = STATE_DIR + "/zlecenia"         # zapisuje usługa raportu, czyta i usuwa usługa akcji (root)
RESULTS_DIR = STATE_DIR + "/wyniki"        # zapisuje usługa akcji, usuwa usługa raportu po potwierdzeniu z Denali
JOB_NAME = re.compile(r"^(\d{1,12})\.json$")
OUTPUT_MAX = 3000
RESULT_MAX = 20000          # wyjście gotowego polecenia bywa dłuższe niż wynik apt
# Akcje, które agent umie wykonać. Wykona tylko te, które są też na lokalnej liście w config.json („actions”).
ACTIONS = {"apt_upgrade": "apt-get update i apt-get upgrade (bez usuwania pakietów, stare pliki konfiguracji zostają)",
           "reboot": "restart maszyny za minutę (shutdown -r +1); po aktualizacji z panelu — tylko gdy jest wymagany",
           "polecenia": "gotowe polecenia tylko do odczytu z listy COMMANDS (dyski, pamięć, usługi, logi, Docker)"}
# Panel podaje tylko klucz — treść polecenia jest wyłącznie tutaj. Lista dla ludzi: denali/polecenia.py.
COMMANDS = {"dyski": ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"],
            "urzadzenia": ["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL"],
            "pamiec": ["free", "-h"],
            "czas_pracy": ["uptime"],
            "procesy": ["ps", "-eo", "pid,user,pcpu,pmem,comm", "--sort=-pcpu"],
            "uslugi_bledy": ["systemctl", "--failed", "--no-pager"],
            "logi_bledy": ["journalctl", "-p", "err", "-n", "50", "--no-pager"],
            "docker": ["docker", "ps", "-a"],
            "porty": ["ss", "-tulpn"],
            "adresy": ["ip", "-br", "address"]}
REBOOT_DELAY = "+1"         # minuta: wynik zdąży trafić do pliku, a raport zwykle jeszcze przed restartem
UNIT_DIR = "/etc/systemd/system"
INTERVAL = 60
APT_EVERY = 3600
NFS_TIMEOUT = 8
HIDDEN_TIMEOUT = 600
HIDDEN_MAX_ENTRIES = 100000
NFS_FS = ("nfs", "nfs4")
SKIP_IFACES = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "cali", "vxlan", "hassio", "tailscale", "wg",
               "zt", "tun", "tap")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


# ───────────────────────── narzędzia ─────────────────────────
def run(cmd, timeout=30, env=None):
    """(kod, stdout, stderr); brak programu = 127. Zawsze z LC_ALL=C — wyjście w stałym formacie."""
    if shutil.which(cmd[0]) is None:
        return 127, "", cmd[0] + ": nie znaleziono"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL="C", LANG="C", **(env or {})))
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "przekroczony czas ({} s)".format(timeout)
    except OSError as e:
        return 126, "", str(e)


_pending = {}


def guarded(key, fn, timeout, *args):
    """fn w wątku z limitem czasu: ("ok", wynik) | ("error", wyjątek) | ("timeout", None) | ("hung", None).
    Zawieszony NFS blokuje wątek na zawsze — wtedy kolejne próby dla tego samego montażu są pomijane („hung”),
    zamiast mnożyć zawieszone wątki."""
    t = _pending.get(key)
    if t is not None and t.is_alive():
        return "hung", None
    box = {}

    def target():
        try:
            box["v"] = fn(*args)
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=target, daemon=True)
    _pending[key] = t
    t.start()
    t.join(timeout)
    if t.is_alive():
        return "timeout", None
    _pending.pop(key, None)
    if "e" in box:
        return "error", box["e"]
    return "ok", box.get("v")


def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


# ───────────────────────── system ─────────────────────────
def os_name():
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', read("/etc/os-release"), re.M)
    return m.group(1) if m else ""


def virt():
    rc, out, _ = run(["systemd-detect-virt"], timeout=10)
    return out.strip() or ("none" if rc == 1 else "")


def addresses():
    """IPv4 i MAC fizycznych interfejsów (bez Dockera, mostków, VPN) — po nich Denali łączy agenta z VM z Proxmoxa."""
    out = []
    rc, so, _ = run(["ip", "-j", "addr"], timeout=10)
    try:
        ifaces = json.loads(so) if rc == 0 else []
    except ValueError:
        ifaces = []
    for i in ifaces:
        name = i.get("ifname", "")
        if not name or name.startswith(SKIP_IFACES):
            continue
        mac = (i.get("address") or "").lower()
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) and mac != "00:00:00:00:00:00":
            out.append({"kind": "mac", "value": mac})
        for a in i.get("addr_info") or []:
            if a.get("family") == "inet" and a.get("local") and not a["local"].startswith("127."):
                out.append({"kind": "ip", "value": a["local"]})
    return out


def _version_key(v):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v)]


def kernel():
    """Uruchomione jądro i najnowsze zainstalowane (/boot). W kontenerze LXC /boot jest pusty — wtedy bez porównania."""
    running = os.uname().release
    try:
        installed = [f[len("vmlinuz-"):] for f in os.listdir("/boot") if f.startswith("vmlinuz-")]
    except OSError:
        installed = []
    newest = max(installed, key=_version_key) if installed else ""
    return {"running": running, "newest": newest}


def reboot():
    for base in ("/run", "/var/run"):
        if os.path.exists(base + "/reboot-required"):
            pkgs = [p for p in read(base + "/reboot-required.pkgs").split() if p]
            return {"required": True, "packages": sorted(set(pkgs))[:20]}
    return {"required": False, "packages": []}


# ───────────────────────── apt ─────────────────────────
APT_RX = re.compile(r"^(\S+?)/(\S+)\s+(\S+)\s+(\S+)(?:\s+\[upgradable from: ([^\]]+)\])?")
_apt_cache = {"t": 0, "v": None}


def parse_apt(text):
    pkgs = []
    for line in text.splitlines():
        m = APT_RX.match(line.strip())
        if m:
            pkgs.append({"name": m.group(1), "new": m.group(3), "old": m.group(5) or "",
                         "security": "security" in m.group(2)})
    return pkgs


def _lists_age_h(now):
    d = "/var/lib/apt/lists"
    try:
        times = [os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d) if f.endswith(("Release", "InRelease"))]
    except OSError:
        return None
    return round((now - max(times)) / 3600, 1) if times else None


def apt(now=None):
    """Lista do aktualizacji z apt (z cache apt — samo `apt update` robi system, np. apt-daily.timer). Raz na godzinę."""
    now = now or time.time()
    if _apt_cache["v"] is not None and now - _apt_cache["t"] < APT_EVERY:
        return _apt_cache["v"]
    rc, so, err = run(["apt", "list", "--upgradable"], timeout=120)
    if rc == 127:
        v = {"error": "brak apt (to nie Debian ani Ubuntu?)"}
    elif rc != 0:
        v = {"error": (err.strip().splitlines() or ["apt zakończył się błędem"])[-1][:200]}
    else:
        v = {"packages": parse_apt(so)[:1000], "lists_age_h": _lists_age_h(now), "checked": now}
    _apt_cache.update(t=now, v=v)
    return v


# ───────────────────────── NFS ─────────────────────────
def parse_mounts(text):
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4:
            out.append({"source": p[0], "mount": p[1].replace("\\040", " "), "fstype": p[2], "opts": p[3]})
    return out


def parse_fstab_nfs(text):
    out = []
    for line in text.splitlines():
        p = line.split("#", 1)[0].split()
        if len(p) >= 3 and p[2] in NFS_FS:
            opts = p[3] if len(p) > 3 else ""
            out.append({"source": p[0], "mount": p[1].replace("\\040", " "), "fstype": p[2], "opts": opts,
                        "noauto": "noauto" in opts.split(",") and "x-systemd.automount" not in opts})
    return out


def _probe(mount):
    os.statvfs(mount)
    os.listdir(mount)
    return True


def nfs(mounts_text=None, fstab_text=None, hidden=None):
    mounts = parse_mounts(read("/proc/mounts") if mounts_text is None else mounts_text)
    fstab = parse_fstab_nfs(read("/etc/fstab") if fstab_text is None else fstab_text)
    hidden = load_hidden() if hidden is None else hidden
    mounted = {m["mount"]: m for m in mounts if m["fstype"] in NFS_FS}
    automount = {m["mount"] for m in mounts if m["fstype"] == "autofs"}
    expected = {}
    for e in fstab:
        if not e["noauto"] or e["mount"] in mounted:
            expected[e["mount"]] = e
    for mnt, m in mounted.items():
        expected.setdefault(mnt, m)
    out = []
    for mnt, e in sorted(expected.items()):
        row = {"mount": mnt, "source": (mounted.get(mnt) or e)["source"], "mounted": mnt in mounted or mnt in automount,
               "in_fstab": any(f["mount"] == mnt for f in fstab)}
        if row["mounted"]:
            opts = (mounted.get(mnt) or e).get("opts", "")
            row["readonly"] = "ro" in opts.split(",")
            status, _ = guarded("nfs:" + mnt, _probe, NFS_TIMEOUT, mnt)
            row["responding"] = status == "ok"
            if status != "ok":
                row["error"] = status
        else:
            try:
                row["local_entries"] = len(os.listdir(mnt)) if os.path.isdir(mnt) else 0
            except OSError:
                row["local_entries"] = None
        if mnt in hidden:
            row["hidden"] = hidden[mnt]
        out.append(row)
    return out


def load_hidden():
    try:
        data = json.loads(read(HIDDEN_FILE, "{}"))
    except ValueError:
        return {}
    checked = data.get("checked")
    return {m: dict(v, checked=checked) for m, v in (data.get("mounts") or {}).items() if isinstance(v, dict)}


def _count(path):
    entries = size = 0
    stack = [path]
    while stack and entries < HIDDEN_MAX_ENTRIES:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    entries += 1
                    try:
                        st = e.stat(follow_symlinks=False)
                        size += st.st_size
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return {"entries": entries, "bytes": size}


def hidden_check():
    """Root, bez sieci: co leży w katalogach montażu NFS „pod spodem”. Bind-mount / (bez podmontowanych systemów
    plików) pokazuje lokalny katalog, który zasłania udział. Wynik do HIDDEN_FILE dla usługi raportu."""
    if os.geteuid() != 0:
        sys.exit("To sprawdzenie wymaga roota (uruchamia je denali-agent-ukryte.timer).")
    result = {}
    nfs_mounts = sorted({m["mount"] for m in parse_mounts(read("/proc/mounts")) if m["fstype"] in NFS_FS})
    if nfs_mounts:
        tmp = tempfile.mkdtemp(prefix="denali-ukryte-")
        try:
            rc, _, err = run(["mount", "--bind", "-o", "ro", "/", tmp], timeout=30)
            if rc != 0:
                result = {m: {"error": (err.strip() or "mount --bind nie powiódł się")[:200]} for m in nfs_mounts}
            else:
                try:
                    for m in nfs_mounts:
                        under = os.path.join(tmp, m.lstrip("/"))
                        status, v = guarded("ukryte:" + m, _count, HIDDEN_TIMEOUT, under)
                        result[m] = v if status == "ok" else {"error": status}
                finally:
                    run(["umount", tmp], timeout=30)
        finally:
            try:
                os.rmdir(tmp)
            except OSError:
                pass
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=STATE_DIR)
    with os.fdopen(fd, "w") as f:
        json.dump({"checked": time.time(), "mounts": result}, f)
    os.chmod(path, 0o644)
    os.replace(path, HIDDEN_FILE)
    log("dane pod montażami: " + (", ".join("{} {}".format(m, v.get("entries", v.get("error"))) for m, v in result.items())
                                  or "brak montaży NFS"))


# ───────────────────────── akcje ─────────────────────────
def _write_json(directory, name, data, mode=0o640, tmp_dir=None):
    """Zapis atomowy: plik tymczasowy (O_EXCL) i zmiana nazwy — dowiązanie podłożone pod tą nazwą zostaje zastąpione,
    a nie zapisane przez nie."""
    fd, tmp = tempfile.mkstemp(dir=tmp_dir or directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, mode)
        os.replace(tmp, os.path.join(directory, name))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path, limit=64 * 1024):
    """Plik JSON bez podążania za dowiązaniem i z limitem wielkości."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("za duży plik")
    return json.loads(data)


def write_jobs(jobs, jobs_dir=None, tmp_dir=None):
    """Zlecenia z odpowiedzi Denali → pliki dla usługi akcji. Tu nic się nie wykonuje."""
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict) or not isinstance(j.get("id"), int) or j.get("action") not in ACTIONS:
            continue
        try:
            job = {"id": j["id"], "action": j["action"]}
            params = j["params"] if isinstance(j.get("params"), dict) else {}
            if params.get("if_required") is True:          # agent przyjmuje tylko te dwa parametry
                job["params"] = {"if_required": True}
            elif isinstance(params.get("command"), str) and params["command"] in COMMANDS:
                job["params"] = {"command": params["command"]}
            # plik tymczasowy w katalogu wyników, żeby w katalogu zleceń pojawił się od razu cały
            _write_json(jobs_dir or JOBS_DIR, "{}.json".format(j["id"]), job, tmp_dir=tmp_dir or RESULTS_DIR)
            log("zlecenie {} z Denali: {}".format(j["id"], j["action"]))
        except OSError as e:
            log("nie da się zapisać zlecenia {}: {}".format(j["id"], e))


def read_results(results_dir=None):
    d = results_dir or RESULTS_DIR
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        m = JOB_NAME.match(name)
        if not m:
            continue
        try:
            r = _read_json(os.path.join(d, name))
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("id") == int(m.group(1)):
            out.append(r)
    return out[:50]


def drop_results(ids, results_dir=None):
    for i in ids if isinstance(ids, list) else []:
        if isinstance(i, int):
            try:
                os.remove(os.path.join(results_dir or RESULTS_DIR, "{}.json".format(i)))
            except OSError:
                pass


def _tail(*texts):
    return "\n".join(t.strip() for t in texts if t and t.strip())[-OUTPUT_MAX:]


def apt_upgrade(params=None):
    env = {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none", "NEEDRESTART_MODE": "a"}
    rc, so, err = run(["apt-get", "update"], timeout=900, env=env)
    if rc != 0:
        return False, rc, _tail("apt-get update:", so, err)
    rc, so2, err2 = run(["apt-get", "-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
                         "upgrade"], timeout=3600, env=env)
    return rc == 0, rc, _tail(so2, err2)


def reboot_required_now():
    """Wymagany restart: plik reboot-required albo nowsze jądro niż uruchomione (to samo, co raport)."""
    k = kernel()
    return reboot()["required"] or bool(k["running"] and k["newest"] and k["running"] != k["newest"])


def reboot_machine(params=None):
    if (params or {}).get("if_required") and not reboot_required_now():
        return True, 0, "Restart niepotrzebny — maszyna go nie wymaga."
    rc, so, err = run(["shutdown", "-r", REBOOT_DELAY, "Denali: restart zlecony z panelu"], timeout=30)
    if rc != 0:
        return False, rc, _tail("shutdown:", so, err)
    return True, 0, _tail("Restart za minutę (shutdown -r {}).".format(REBOOT_DELAY), so, err)


def run_command(params=None):
    argv = COMMANDS.get((params or {}).get("command"))
    if not argv:
        return False, None, "Agent nie zna tego polecenia (zainstaluj nowszą wersję poleceniem z panelu)."
    rc, so, err = run(argv, timeout=30)
    return rc == 0, rc, "\n".join(t.strip("\n") for t in (so, err) if t and t.strip())[:RESULT_MAX]


RUNNERS = {"apt_upgrade": apt_upgrade, "reboot": reboot_machine, "polecenia": run_command}


def do_jobs(cfg, jobs_dir=None, results_dir=None, runners=None):
    """Usługa akcji (root): każde zlecenie z katalogu — sprawdź lokalną listę, wykonaj, zapisz wynik."""
    jobs_dir, results_dir, runners = jobs_dir or JOBS_DIR, results_dir or RESULTS_DIR, runners or RUNNERS
    allowed = set(a for a in (cfg.get("actions") or []) if a in ACTIONS)
    try:
        names = sorted(os.listdir(jobs_dir))
    except OSError:
        return
    for name in names:
        path = os.path.join(jobs_dir, name)
        m = JOB_NAME.match(name)
        job = None
        if m:
            try:
                job = _read_json(path)
            except (OSError, ValueError):
                job = None
        try:
            os.remove(path)        # zlecenie wykonuje się najwyżej raz, także gdy coś pójdzie nie tak
        except OSError:
            pass
        if not m or not isinstance(job, dict) or job.get("id") != int(m.group(1)):
            continue
        jid, action, started = job["id"], job.get("action"), time.time()
        if action not in allowed:
            out = "Akcja „{}” nie jest dozwolona na tej maszynie (lokalna lista: {}). Pozwól: sudo python3 {} pozwol {}" \
                .format(action, ", ".join(sorted(allowed)) or "pusta", PROGRAM, action)
            _write_json(results_dir, name, {"id": jid, "state": "blad", "rc": None, "output": out,
                                            "started": started, "finished": time.time()}, 0o644)
            log("zlecenie {} odrzucone: {} spoza lokalnej listy".format(jid, action))
            continue
        _write_json(results_dir, name, {"id": jid, "state": "trwa", "started": started}, 0o644)
        log("zlecenie {}: {} — start".format(jid, action))
        try:
            ok, rc, out = runners[action](job.get("params") if isinstance(job.get("params"), dict) else {})
        except Exception as e:  # noqa: BLE001
            ok, rc, out = False, None, "{}: {}".format(type(e).__name__, e)
        _write_json(results_dir, name, {"id": jid, "state": "ok" if ok else "blad", "rc": rc,
                                        "output": out[-RESULT_MAX:], "started": started, "finished": time.time()}, 0o644)
        log("zlecenie {}: {} — {}".format(jid, action, "ok" if ok else "błąd (kod {})".format(rc)))


def run_jobs():
    if os.geteuid() != 0:
        sys.exit("Akcje wykonuje usługa denali-agent-akcje (root).")
    do_jobs(load_config())


def set_allowed(action, allow, path=CONFIG_PATH):
    """pozwol / zabron: lokalna lista dozwolonych akcji (tylko root może ją zmienić)."""
    _need_root()
    cfg = load_config(path)
    have = set(cfg.get("actions") or [])
    have = have | {action} if allow else have - {action}
    cfg["actions"] = sorted(have)
    _save_config(cfg, path)
    _systemctl("restart", "denali-agent.service")      # raport poda Denali aktualną listę
    print("Lokalna lista dozwolonych akcji: {}".format(", ".join(cfg["actions"]) or "pusta"))
    if allow and not os.path.exists(os.path.join(UNIT_DIR, "denali-agent-akcje.path")):
        print("UWAGA: brak usługi denali-agent-akcje.path (agent sprzed wersji 1.1) — zainstaluj agenta ponownie "
              "poleceniem z Denali.")


# ───────────────────────── raport ─────────────────────────
def collect():
    def safe(fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — jeden zepsuty odczyt nie może zatrzymać raportu
            log("odczyt {}: {}: {}".format(fn.__name__, type(e).__name__, e))
            return default
    return {"version": VERSION, "ts": time.time(), "hostname": socket.gethostname(), "os": safe(os_name, ""),
            "virt": safe(virt, ""), "addresses": safe(addresses, []), "kernel": safe(kernel, {}),
            "reboot": safe(reboot, {}), "apt": safe(apt, {"error": "odczyt nieudany"}), "nfs": safe(nfs, [])}


def _ssl(cfg):
    return ssl.create_default_context(cafile=cfg.get("ca_file") or None)


def post(cfg, path, payload, token=None, timeout=30):
    """(kod HTTP, odpowiedź JSON). Błąd sieci → OSError / URLError."""
    req = urllib.request.Request(cfg["server"].rstrip("/") + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "denali-agent/" + VERSION})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    ctx = _ssl(cfg) if cfg["server"].startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        return e.code, body


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


def loop(cfg):
    log("agent Denali {} → {} co {} s".format(VERSION, cfg["server"], INTERVAL))
    while True:
        started = time.monotonic()
        wait = INTERVAL
        try:
            payload = collect()
            payload["actions"] = [a for a in (cfg.get("actions") or []) if a in ACTIONS]
            payload["jobs"] = read_results()
            status, body = post(cfg, "/api/agent/v1/raport", payload, token=cfg["token"])
            if status == 200:
                wait = int(body.get("interval") or INTERVAL)
                write_jobs(body.get("jobs"))
                ack = body.get("ack") or []
                drop_results(ack)
                if ack:
                    _apt_cache["t"] = 0         # po akcji świeża lista pakietów w następnym raporcie
            elif status == 401:
                log("Denali odrzuciło token: {} — ponowię za 10 min".format(body.get("error", "")))
                wait = 600
            else:
                log("Denali odpowiedziało {}: {}".format(status, body.get("error", "")))
        except (OSError, ValueError) as e:
            log("Denali nieosiągalne ({}) — ponowię za {} s".format(e, INTERVAL))
        time.sleep(max(10, wait - (time.monotonic() - started)))


# ───────────────────────── instalacja ─────────────────────────
SERVICE = """[Unit]
Description=Denali: agent (pakiety apt, wymagany restart, montaże NFS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
ExecStart=/usr/bin/python3 {program} run
Restart=always
RestartSec=30
# tylko czyta: system, /home i urządzenia niedostępne do zapisu; bez nowych uprawnień.
# Zapis tylko do katalogów zleceń i wyników — wykonuje je osobna usługa (root) i tylko z lokalnej listy.
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=-{jobs} -{results}
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
MemoryMax=128M
Nice=10

[Install]
WantedBy=multi-user.target
"""

HIDDEN_SERVICE = """[Unit]
Description=Denali: dane pod montażami NFS (raz dziennie, bez sieci)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {program} ukryte
# root jest potrzebny tylko do bind-mountu; sieć niepotrzebna, więc odcięta
PrivateNetwork=yes
PrivateTmp=yes
NoNewPrivileges=yes
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=15min
"""

HIDDEN_TIMER = """[Unit]
Description=Denali: dane pod montażami NFS — raz dziennie

[Timer]
OnBootSec=15min
OnUnitActiveSec=1d
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
"""

ACTIONS_PATH = """[Unit]
Description=Denali: czeka na zlecenie akcji z panelu

[Path]
DirectoryNotEmpty={jobs}

[Install]
WantedBy=paths.target
"""

ACTIONS_SERVICE = """[Unit]
Description=Denali: akcja zlecona z panelu — tylko z lokalnej listy dozwolonych (root)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {program} akcje
TimeoutStartSec=2h
Nice=5
"""

UNITS = {"denali-agent.service": SERVICE, "denali-agent-ukryte.service": HIDDEN_SERVICE,
         "denali-agent-ukryte.timer": HIDDEN_TIMER, "denali-agent-akcje.path": ACTIONS_PATH,
         "denali-agent-akcje.service": ACTIONS_SERVICE}


def unit_text(name):
    return UNITS[name].format(user=USER, program=PROGRAM, jobs=JOBS_DIR, results=RESULTS_DIR)


def _need_root():
    if os.geteuid() != 0:
        sys.exit("Uruchom przez sudo.")


def _systemctl(*args):
    rc, _, err = run(["systemctl"] + list(args), timeout=60)
    if rc != 0:
        log("systemctl {}: {}".format(" ".join(args), err.strip()))
    return rc == 0


def _save_config(cfg, path=CONFIG_PATH):
    import grp
    gid = grp.getgrnam(USER).gr_gid
    fd = os.open(path + ".nowy", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chown(path + ".nowy", 0, gid)
    os.chmod(path + ".nowy", 0o640)
    os.replace(path + ".nowy", path)


def install(server, code, ca_file="", allow=None):
    import grp
    import pwd
    _need_root()
    if sys.version_info < (3, 9):
        sys.exit("Potrzebny Python 3.9 albo nowszy.")
    if not os.path.isdir("/run/systemd/system"):
        sys.exit("Ta maszyna nie działa na systemd — agent jej nie obsługuje.")
    if not re.match(r"^https://[^/\s]+/?$", server):
        sys.exit("Adres Denali: https://nazwa (bez ścieżki), tak jak w poleceniu z panelu.")
    if os.path.exists(UNIT_DIR + "/hlc-agent.service"):
        print("UWAGA: na tej maszynie jest stary agent HLC (usługa hlc-agent, działa jako root).\n"
              "       Denali go nie używa. Usuń go: sudo python3 /opt/hlc-agent/agent.py --uninstall\n")
    if allow is None:       # ponowna instalacja zostawia lokalną listę akcji taką, jaka była
        try:
            allow = load_config().get("actions") or []
        except (OSError, ValueError):
            allow = []
    bad = [a for a in allow if a not in ACTIONS]
    if bad:
        sys.exit("Nieznana akcja: {}. Dostępne: {}".format(bad[0], ", ".join(sorted(ACTIONS))))
    cfg = {"server": server.rstrip("/"), "ca_file": ca_file, "actions": sorted(set(allow))}
    try:
        status, body = post(cfg, "/api/agent/v1/rejestracja", {"code": code, "hostname": socket.gethostname()})
    except (OSError, ValueError) as e:
        sys.exit("Nie da się połączyć z {}: {}".format(server, e))
    if status != 200 or not body.get("token"):
        sys.exit("Denali odrzuciło rejestrację ({}): {}".format(status, body.get("error", "")))
    cfg["token"] = body["token"]

    try:
        pwd.getpwnam(USER)
    except KeyError:
        rc, _, err = run(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent",
                          "--shell", "/usr/sbin/nologin", USER])
        if rc != 0:
            sys.exit("Nie udało się założyć użytkownika {}: {}".format(USER, err.strip()))
    gid = grp.getgrnam(USER).gr_gid
    os.makedirs(INSTALL_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o755)
    uid = pwd.getpwnam(USER).pw_uid
    for d in (JOBS_DIR, RESULTS_DIR):
        os.makedirs(d, exist_ok=True)
        os.chown(d, uid, gid)
        os.chmod(d, 0o750)
    if os.path.abspath(__file__) != PROGRAM:
        shutil.copyfile(os.path.abspath(__file__), PROGRAM)
    os.chmod(PROGRAM, 0o755)
    _save_config(cfg)
    for name in UNITS:
        with open(os.path.join(UNIT_DIR, name), "w") as f:
            f.write(unit_text(name))
    _systemctl("daemon-reload")
    _systemctl("enable", "denali-agent.service", "denali-agent-ukryte.timer", "denali-agent-akcje.path")
    _systemctl("restart", "denali-agent.service")
    _systemctl("start", "denali-agent-ukryte.timer", "denali-agent-akcje.path")
    _systemctl("start", "--no-block", "denali-agent-ukryte.service")
    print("Gotowe. Agent wysyła raport co minutę — maszyna pojawi się w Denali za chwilę.\n"
          "Stan: systemctl status denali-agent · dziennik: journalctl -u denali-agent -n 30\n"
          "Dozwolone akcje na tej maszynie: {}".format(
              ", ".join(cfg["actions"]) or "żadne (pozwól: sudo python3 {} pozwol apt_upgrade)".format(PROGRAM)))


def uninstall():
    _need_root()
    _systemctl("disable", "--now", "denali-agent.service", "denali-agent-ukryte.timer", "denali-agent-akcje.path")
    _systemctl("stop", "denali-agent-ukryte.service", "denali-agent-akcje.service")
    for name in UNITS:
        try:
            os.remove(os.path.join(UNIT_DIR, name))
        except OSError:
            pass
    _systemctl("daemon-reload")
    for d in (INSTALL_DIR, os.path.dirname(CONFIG_PATH), STATE_DIR):
        shutil.rmtree(d, ignore_errors=True)
    run(["userdel", USER])
    print("Agent usunięty z tej maszyny. Usuń go też w Denali (Integracje → agent → Usuń), żeby unieważnić token.")


def main():
    ap = argparse.ArgumentParser(description="Agent Denali")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="rejestracja kodem z panelu i instalacja usług")
    i.add_argument("server")
    i.add_argument("code")
    i.add_argument("--ca-file", default="", help="własny certyfikat CA (gdy Denali nie ma certyfikatu Let's Encrypt)")
    i.add_argument("--akcje", default=None,
                   help="lokalna lista dozwolonych akcji, np. apt_upgrade,reboot (domyślnie pusta)")
    for cmd, text in (("pozwol", "dopisz akcję do lokalnej listy dozwolonych"), ("zabron", "usuń akcję z listy")):
        p = sub.add_parser(cmd, help=text)
        p.add_argument("akcja", choices=sorted(ACTIONS))
    sub.add_parser("akcje", help="wykonaj zlecenia z panelu (usługa denali-agent-akcje, root)")
    sub.add_parser("uninstall")
    sub.add_parser("run", help="raport co minutę (usługa denali-agent)")
    sub.add_parser("ukryte", help="dane pod montażami NFS (usługa denali-agent-ukryte, root)")
    sub.add_parser("print", help="raport na ekran, bez wysyłania")
    a = ap.parse_args()
    if a.cmd == "install":
        allow = None if a.akcje is None else [x for x in a.akcje.replace(",", " ").split() if x]
        install(a.server, a.code, a.ca_file, allow)
    elif a.cmd in ("pozwol", "zabron"):
        set_allowed(a.akcja, a.cmd == "pozwol")
    elif a.cmd == "akcje":
        run_jobs()
    elif a.cmd == "uninstall":
        uninstall()
    elif a.cmd == "run":
        loop(load_config())
    elif a.cmd == "ukryte":
        hidden_check()
    elif a.cmd == "print":
        print(json.dumps(collect(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
