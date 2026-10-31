"""Agent na maszynie (Etap 4): tylko tam, gdzie API nie wystarcza — pakiety apt, „wymagany restart”, montaże NFS.

Każdy agent to osobna integracja typu „agent” (usunięcie integracji = unieważnienie agenta). Agent sam wysyła raport
co minutę; serwer niczego mu nie zleca (akcje dopiero w Etapie 5, z białej listy na maszynie). Agent podaje surowe
fakty, a ocena (co jest „uwagą”, kiedy restart jest wymagany) dzieje się tutaj — agent zostaje mały i głupi.

Rejestracja: administrator generuje jednorazowy kod (ważny godzinę), polecenie instalacji wymienia go na token.
W bazie jest tylko skrót tokenu.
"""
import hashlib
import hmac
import ipaddress
import secrets
import time
from datetime import datetime

from sqlalchemy import select

from . import discovery, integrations
from .adapters import Adapter, AdapterError, Found, PollResult, State, Update
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


class AgentError(ValueError):
    pass


class Agent(Adapter):
    """Wpis w rejestrze integracji. Agent nie jest odpytywany — sam wysyła raporty (push)."""
    type = TYPE
    label = "Agent na maszynie"
    push = True
    fields = []
    default_poll = REPORT_EVERY

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
        "apt_lists_age_h": apt.get("lists_age_h"), "problems": problems, "reported_at": now}))

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
