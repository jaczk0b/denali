"""Agent na maszynie (Etap 4): tylko tam, gdzie API nie wystarcza — pakiety apt, „wymagany restart”, montaże NFS.

Każdy agent to osobna integracja typu „agent” (usunięcie integracji = unieważnienie agenta). Agent sam wysyła raport
co minutę; w odpowiedzi może dostać zlecenie akcji (Etap 5) — wykona je tylko, jeśli akcja jest na jego lokalnej
białej liście (plik konfiguracji roota na maszynie; panel jej nie zmieni). Agent podaje surowe fakty, a ocena
(co jest „uwagą”, kiedy restart jest wymagany) dzieje się tutaj — agent zostaje mały i głupi.

Rejestracja: administrator generuje jednorazowy kod (ważny godzinę), polecenie instalacji wymienia go na token.
W bazie jest tylko skrót tokenu.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from . import discovery, integrations
from .adapters import ActionSpec, Adapter, AdapterError, Found, PollResult, State, Update
from .db import kv_get, kv_set
from .models import external_ref, integration, observed_state

TYPE = "agent"
ENROLL_KEY = "agent.enroll"
ENROLL_TTL = 3600
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # bez 0/O i 1/I
REPORT_EVERY = 60
SILENT_AFTER = 5 * 60          # bez raportu dłużej = stan nieznany (maszyna leży albo agent stoi — ping powie które)
HIDDEN_MAX_AGE = 3 * 86400     # sprawdzenie „danych pod montażem” (raz dziennie) starsze niż to = nieaktualne
VIRT_KIND = {"kvm": "vm", "qemu": "vm", "vmware": "vm", "microsoft": "vm", "xen": "vm", "oracle": "vm",
             "lxc": "lxc", "lxc-libvirt": "lxc", "none": "host"}


AGENT_ACTIONS = ("apt_upgrade", "reboot", "polecenia", "clean_cores", "nfs_remount", "docker_prune", "journal_vacuum",
                 "db_backup")    # akcje, które agent umie wykonać (jego lokalna lista może zawierać tylko te)


class AgentError(ValueError):
    pass


class Agent(Adapter):
    """Wpis w rejestrze integracji. Agent nie jest odpytywany — sam wysyła raporty (push)."""
    type = TYPE
    label = "Agent na maszynie"
    push = True
    fields = []
    default_poll = REPORT_EVERY
    actions = [
        ActionSpec("apt_upgrade", "Zainstaluj aktualizacje (apt upgrade)",
                   "Agent na maszynie wykona apt-get update i apt-get upgrade (bez usuwania pakietów, z zachowaniem "
                   "zmienionych plików konfiguracji). Usługi z aktualizowanych pakietów mogą się zrestartować. "
                   "Maszyna sama się nie zrestartuje.", ("host",), True),
        ActionSpec("reboot", "Uruchom ponownie maszynę",
                   "Agent zrestartuje system za minutę (shutdown -r +1). Wszystko, co działa na maszynie, przez kilka "
                   "minut nie będzie dostępne. Na VM z Proxmoxa to samo robi „Uruchom ponownie” z Proxmoxa.",
                   ("host",), True),
        ActionSpec("clean_cores", "Usuń pliki core z kontenerów",
                   "Agent sam znajdzie w kontenerach pliki core (zrzuty pamięci) i usunie tylko je: zwykłe pliki o nazwie "
                   "core, wykryte w warstwie zapisywalnej. Panel nie podaje ścieżek. Kontenery nie są restartowane.",
                   ("host",), True),
        ActionSpec("nfs_remount", "Zamontuj ponownie NFS",
                   "Agent zamontuje udziały NFS z /etc/fstab, które są odłączone albo nie odpowiadają. Działające zostają "
                   "bez zmian. Program piszący do zawieszonego udziału może dostać błąd zapisu.", ("host",), True),
        ActionSpec("docker_prune", "Usuń nieużywane obrazy Dockera",
                   "Agent usunie obrazy bez nazwy (dangling) i cache budowania. Obrazy w użyciu i wolumeny zostają.",
                   ("host",), True),
        ActionSpec("journal_vacuum", "Przytnij dziennik systemowy",
                   "Agent zmniejszy dziennik systemd do 200 MB. Starsze wpisy znikną, usługi działają dalej.", ("host",)),
        ActionSpec("db_backup", "Kopia baz Postgres",
                   "Agent zrobi pg_dumpall z każdego kontenera Postgres do /var/backups/denali-agent (5 ostatnich na "
                   "kontener). Kopia zostaje na maszynie, do panelu nie trafia.", ("host",)),
        ActionSpec("polecenia", "Gotowe polecenia (tylko odczyt)",
                   "Agent uruchomi jedno z poleceń z listy w kodzie agenta (dyski, pamięć, usługi, logi). "
                   "Dowolnego polecenia panel nie może zlecić.", ("host",), terminal=True),
    ]

    async def test(self):
        raise AdapterError("Agent sam wysyła raport co minutę — nie ma czego sprawdzać z tej strony.")

    async def poll(self):
        raise AdapterError("Agent sam wysyła raport co minutę.")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_code(code):
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


# ───────────────────────── rejestracja ─────────────────────────
def new_code(conn, user, now=None):
    """Jednorazowy kod do polecenia instalacji. Zwraca kod w postaci XXXXX-XXXXX."""
    now = now or time.time()
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
    pending = [p for p in kv_get(conn, ENROLL_KEY, []) if p["expires"] > now]
    pending.append({"hash": _hash(code), "expires": now + ENROLL_TTL, "by": user})
    kv_set(conn, ENROLL_KEY, pending[-10:])
    return f"{code[:5]}-{code[5:]}"


def _clean_hostname(name):
    name = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_.")[:63]
    if not name:
        raise AgentError("Brak nazwy maszyny.")
    return name


def agent_rows(conn):
    return conn.execute(select(integration).where(integration.c.type == TYPE)).mappings().all()


def enroll(conn, code, hostname, now=None):
    """Kod → token. Ta sama maszyna zainstalowana ponownie dostaje nowy token w tej samej integracji
    (zostaje przypięcie do urządzenia i historia). Zwraca (integration_id, token)."""
    now = now or time.time()
    hostname = _clean_hostname(hostname)
    h = _hash(_clean_code(code))
    pending = [p for p in kv_get(conn, ENROLL_KEY, []) if p["expires"] > now]
    match = next((p for p in pending if hmac.compare_digest(p["hash"], h)), None)
    if not match:
        raise AgentError("Kod jest zły albo wygasł (ważny godzinę, jednorazowy). Wygeneruj nowy w Denali.")
    kv_set(conn, ENROLL_KEY, [p for p in pending if p is not match])
    token = secrets.token_urlsafe(32)
    config = {"token_hash": _hash(token), "hostname": hostname, "enrolled_by": match["by"], "enrolled_at": now}
    existing = next((r for r in agent_rows(conn) if (r["config"] or {}).get("hostname") == hostname), None)
    if existing:
        conn.execute(integration.update().where(integration.c.id == existing["id"]).values(
            config={**(existing["config"] or {}), **config}, enabled=True, last_error=""))
        return existing["id"], token
    name = f"agent: {hostname}"
    taken = {r.name for r in conn.execute(select(integration.c.name))}
    n = 2
    while name in taken:
        name, n = f"agent: {hostname} ({n})", n + 1
    iid = conn.execute(integration.insert().values(type=TYPE, name=name, config=config, secret_ct=None,
                                                   poll_seconds=REPORT_EVERY)).inserted_primary_key[0]
    return iid, token


def authenticate(conn, token):
    """Integracja agenta dla tokenu albo None (zły, usunięty agent, wyłączona integracja)."""
    if not token:
        return None
    h = _hash(token)
    for r in agent_rows(conn):
        if hmac.compare_digest((r["config"] or {}).get("token_hash", ""), h):
            return r if r["enabled"] else None
    return None


# ───────────────────────── raport → stan ─────────────────────────
def _s(v, n=200):
    return str(v if v is not None else "")[:n]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


GB = 1024 ** 3
MEMORY_WARN, MEMORY_CRIT = 92, 98
SWAP_WARN, SWAP_CRIT = 80, 95
LAYER_WARN, LAYER_CRIT = 2 * GB, 10 * GB
NFS_SLOW_WARN, NFS_SLOW_CRIT = 500, 3000
DISK_PROBLEM = 85           # od tylu % karta pokazuje „uwagę”; próg powiadomienia bierze się z profilu
REPORT_MAX = 100            # najwięcej dysków, usług i kontenerów branych z raportu


def _system_alarms(report):
    """Dyski, pamięć i usługi z raportu → surowe alarmy. Procenty oceniane są w alerts.py według profilu."""
    out = []
    for d in (report.get("disks") if isinstance(report.get("disks"), list) else [])[:REPORT_MAX]:
        if not isinstance(d, dict):
            continue
        mount, pct, inodes = _s(d.get("mount"), 200), _num(d.get("pct")), _num(d.get("inodes_pct"))
        if pct is not None:
            free = f", wolne {d['free_gb']} GB" if _num(d.get("free_gb")) is not None else ""
            out.append({"rule": "disk", "key": mount, "pct": pct, "text": f"{mount}: dysk zajęty w {pct:.0f}%{free}"})
        if inodes is not None:
            out.append({"rule": "inode", "key": mount, "pct": inodes, "text": f"{mount}: i-węzły zajęte w {inodes:.0f}%"})
    mem = report.get("memory") if isinstance(report.get("memory"), dict) else {}
    for rule, name, warn, crit in (("memory", "RAM", MEMORY_WARN, MEMORY_CRIT), ("swap", "Swap", SWAP_WARN, SWAP_CRIT)):
        pct = _num(mem.get(rule))
        if pct is not None and pct >= warn:
            out.append({"rule": rule, "key": rule, "text": f"{name} zajęty w {pct:.0f}%",
                        "severity": "krytyczny" if pct >= crit else "ostrzezenie"})
    for u in (report.get("failed_units") if isinstance(report.get("failed_units"), list) else [])[:REPORT_MAX]:
        unit = _s(u, 120)
        if unit:
            out.append({"rule": "systemd", "key": unit, "text": f"Usługa {unit} w stanie failed"})
    return out


def _docker_alarms(containers):
    out = []
    for c in containers:
        name = c["name"]
        size = _num(c.get("size_rw")) or 0
        cores = _num(c.get("core_files")) or 0
        if c.get("health") == "unhealthy":
            out.append({"rule": "container_unhealthy", "key": name, "text": f"Kontener {name}: nie przechodzi kontroli zdrowia"})
        if c.get("state") == "restarting":
            out.append({"rule": "container_restarting", "key": name, "text": f"Kontener {name} się restartuje"})
        if c.get("state") == "exited" and c.get("exit_code") and c.get("policy") not in ("no", "", None):
            out.append({"rule": "container_exited", "key": name,
                        "text": f"Kontener {name} zatrzymany z kodem {c['exit_code']}"})
        if size >= LAYER_WARN:
            out.append({"rule": "container_layer", "key": name, "text": f"Kontener {name} pisze do siebie: warstwa {size / GB:.1f} GB",
                        "severity": "krytyczny" if size >= LAYER_CRIT else "ostrzezenie"})
        if cores:
            out.append({"rule": "core_dumps", "key": name, "text": f"Kontener {name}: pliki core ({cores:.0f})"})
    return out


def _containers(report):
    """Lista kontenerów z raportu albo None, gdy agent jej nie podał (starszy agent, brak Dockera)."""
    rows = report.get("containers")
    if not isinstance(rows, list):
        return None
    out = []
    for c in rows[:REPORT_MAX]:
        if isinstance(c, dict) and c.get("name"):
            out.append({"name": _s(c["name"], 100), "image": _s(c.get("image"), 200), "image_id": _s(c.get("image_id"), 80),
                        "state": _s(c.get("state"), 20), "health": _s(c.get("health"), 20), "exit_code": _num(c.get("exit_code")),
                        "policy": _s(c.get("policy"), 20), "restarts": _num(c.get("restarts")),
                        "size_rw": _num(c.get("size_rw")), "core_files": _num(c.get("core_files"))})
    return out


def _nfs_alarms(mounts):
    out = []
    for m in mounts:
        mnt = _s(m.get("mount"), 200)
        ms = _num(m.get("latency_ms"))
        if ms is not None and ms >= NFS_SLOW_WARN:
            out.append({"rule": "nfs_slow", "key": mnt, "text": f"{mnt}: odpowiedź NFS {ms:.0f} ms",
                        "severity": "krytyczny" if ms >= NFS_SLOW_CRIT else "ostrzezenie"})
    return out


def _nfs_server(source):
    """„192.168.1.30:/mnt/tank/media” → {"ip": …} albo {"hostname": …} do zależności „dane”."""
    host = _s(source).rsplit(":", 1)[0].strip("[]") if ":" in _s(source) else ""
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return {"ip": host}
    except ValueError:
        return {"hostname": host.lower()}


def latest_version():
    """Wersja agenta, którą rozdaje ten panel (z pliku agenta)."""
    text = (Path(__file__).resolve().parent / "agent" / "denali-agent.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "([0-9.]+)"', text, re.M)
    return m.group(1) if m else ""


def outdated(version):
    def key(v):
        return tuple(int(x) for x in str(v).split(".") if x.isdigit())
    return bool(version) and key(version) < key(latest_version())


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"


def _mount_problem(m, now):
    """Opis problemu z montażem albo None."""
    mnt = _s(m.get("mount"), 200)
    if not m.get("mounted"):
        if m.get("local_entries"):
            return f"{mnt} niezamontowany, a w katalogu są pliki ({m['local_entries']}) — zapis idzie na lokalny dysk"
        return f"{mnt} niezamontowany"
    if m.get("responding") is False:
        return f"{mnt} nie odpowiada (serwer NFS?)"
    hidden = m.get("hidden") or {}
    if hidden.get("entries") and now - (hidden.get("checked") or 0) < HIDDEN_MAX_AGE:
        size = hidden.get("bytes")
        extra = f", {size / 1024 ** 3:.1f} GB".replace(".", ",") if isinstance(size, (int, float)) and size else ""
        return f"{mnt}: pod montażem leżą stare dane ({hidden['entries']} pozycji{extra}) — zapisane, gdy udział nie był podłączony"
    return None


def to_result(report, now=None):
    """Raport agenta (dane z zewnątrz — sprawdzane) → wynik jak z odczytu integracji."""
    now = now or time.time()
    if not isinstance(report, dict):
        raise AgentError("Raport nie jest obiektem JSON.")
    hostname = _clean_hostname(report.get("hostname"))
    addresses = []
    for a in report.get("addresses") or []:
        if isinstance(a, dict) and a.get("kind") in ("ip", "mac") and a.get("value"):
            addresses.append({"kind": a["kind"], "value": _s(a["value"], 64)})
    mounts = [m for m in (report.get("nfs") or []) if isinstance(m, dict) and m.get("mount")][:50]
    targets = []
    for m in mounts:
        t = _nfs_server(m.get("source"))
        if t and t not in targets:
            targets.append(t)
    deps = [{**t, "type": "dane", "hard": True} for t in targets]
    virt = _s(report.get("virt"), 32).lower()
    out = PollResult()
    out.found.append(Found("host", {"name": hostname, "kind": VIRT_KIND.get(virt, "vm"), "addresses": addresses[:20],
                                    "live_deps": deps}))

    kernel = report.get("kernel") if isinstance(report.get("kernel"), dict) else {}
    reboot = report.get("reboot") if isinstance(report.get("reboot"), dict) else {}
    newer_kernel = bool(kernel.get("running") and kernel.get("newest") and kernel["running"] != kernel["newest"])
    reboot_required = bool(reboot.get("required")) or newer_kernel
    why_reboot = ("nowe jądro " + _s(kernel.get("newest"), 60)) if newer_kernel else \
        ", ".join(_s(p, 60) for p in (reboot.get("packages") or [])[:3]) or ""

    problems = [p for p in (_mount_problem(m, now) for m in mounts) if p]
    containers = _containers(report)
    alarms = [{"rule": "nfs_hidden" if "pod montażem" in p else "nfs", "key": p.split(":")[0].split(" ")[0], "text": p}
              for p in problems]
    alarms += _nfs_alarms(mounts) + _system_alarms(report) + _docker_alarms(containers or [])
    problems += [a["text"] for a in alarms
                 if a["rule"] not in ("nfs", "nfs_hidden", "inode", "disk") or (a.get("pct") or 0) >= DISK_PROBLEM]
    apt = report.get("apt") if isinstance(report.get("apt"), dict) else {}
    parts = [f"agent {_s(report.get('version'), 20)}"]
    if report.get("os"):
        parts.append(_s(report["os"], 60))
    if mounts:
        ok = sum(1 for m in mounts if m.get("mounted") and m.get("responding") is not False)
        parts.append(f"NFS: {ok}/{len(mounts)} w porządku")
    if apt.get("error"):
        parts.append(f"apt: {_s(apt['error'], 80)}")
    elif isinstance(apt.get("lists_age_h"), (int, float)) and apt["lists_age_h"] > 72:
        parts.append(f"lista pakietów nieodświeżana od {apt['lists_age_h'] / 24:.0f} dni")
    if reboot_required:
        parts.append("wymagany restart" + (f" ({why_reboot})" if why_reboot else ""))
    summary = "; ".join(problems) if problems else " · ".join(parts)
    out.states.append(State("host", "warn" if problems else "ok", summary[:300], {
        "reboot_required": reboot_required, "reboot_why": why_reboot, "version": _s(report.get("version"), 20),
        "os": _s(report.get("os"), 100), "kernel": _s(kernel.get("running"), 60), "nfs": mounts,
        "apt_lists_age_h": apt.get("lists_age_h"), "problems": problems, "reported_at": now, "alarms": alarms,
        "boot_id": _s(report.get("boot_id"), 40), "dpkg_changed": _num(report.get("dpkg_changed")),
        "ips": sorted({a["value"] for a in addresses if a["kind"] == "ip"}),
        "disks": [d for d in (report.get("disks") if isinstance(report.get("disks"), list) else [])[:REPORT_MAX]
                  if isinstance(d, dict)],
        "memory": report.get("memory") if isinstance(report.get("memory"), dict) else {},
        "failed_units": [_s(u, 120) for u in (report.get("failed_units") if isinstance(report.get("failed_units"), list)
                                              else [])[:REPORT_MAX]],
        **({"containers": containers} if containers is not None else {}),
        "actions": sorted({a for a in (report.get("actions") if isinstance(report.get("actions"), list) else [])
                            if a in AGENT_ACTIONS})}))

    if isinstance(apt.get("packages"), list) and not apt.get("error"):
        out.updates = [Update("host", _s(p.get("name"), 200), "pakiet", _s(p.get("old"), 100), _s(p.get("new"), 100),
                              bool(p.get("security")))
                       for p in apt["packages"][:1000] if isinstance(p, dict) and p.get("name")]
    return out


def record_report(conn, row, report, now=None):
    """Zapisuje raport. Maszyna z agentem nie czeka w kolejce: zainstalował go administrator, więc od razu
    przypinamy ją do pasującego urządzenia (np. VM z Proxmoxa po IP/MAC) albo tworzymy nowe."""
    now = now or time.time()
    result = to_result(report, now)
    integrations.record(conn, row["id"], result, now)
    ref = conn.execute(select(external_ref).where(external_ref.c.integration_id == row["id"],
                                                  external_ref.c.external_id == "host")).mappings().first()
    if ref and ref["status"] == "nowy":
        match = discovery.suggest_matches(conn).get(ref["id"])
        if match:
            discovery.link(conn, ref["id"], match["id"], user="agent")
        else:
            discovery.approve(conn, ref["id"], user="agent")
        # zależności (NFS → TrueNAS) od razu, nie dopiero przy następnym raporcie
        integrations.record(conn, row["id"], result, now)
    return result


def mark_silent(conn, now=None):
    """Agent bez raportu dłużej niż SILENT_AFTER: stan nieznany i błąd na liście integracji."""
    now = now or time.time()
    for r in agent_rows(conn):
        if not r["enabled"]:
            continue
        last = r["last_ok_at"] or (r["config"] or {}).get("enrolled_at") or now
        if now - last < SILENT_AFTER:
            continue
        msg = f"Agent nie wysłał raportu od {_when(r['last_ok_at'])}" if r["last_ok_at"] else \
            "Agent jeszcze nie wysłał raportu — sprawdź na maszynie: systemctl status denali-agent"
        if r["last_error"] != msg:
            integrations.record_error(conn, r["id"], msg, now)
        conn.execute(observed_state.update().where(observed_state.c.integration_id == r["id"],
                                                   observed_state.c.state != "unknown")
                     .values(state="unknown", summary=msg[:300], observed_at=now))
