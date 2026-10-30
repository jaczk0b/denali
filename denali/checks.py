"""Sprawdzenia z LAN, wyliczane same — bez ustawiania niczego na urządzeniu:

- każde urządzenie z adresem (IP, a gdy go brak — nazwa hosta) dostaje ping,
- każde urządzenie z adresem aplikacji dostaje sprawdzenie HTTP (i datę ważności certyfikatu dla https),
- profil „Monitoruj stan: nie” wyłącza sprawdzenia dla typu albo pojedynczego urządzenia.

Ping działa bez uprawnień roota (gniazdo ICMP „datagramowe”, które Docker domyślnie pozwala otworzyć).
Gdy to niemożliwe, sprawdzamy kilka typowych portów TCP: nawet odmowa połączenia znaczy, że host żyje.
"""
import asyncio
import errno
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from sqlalchemy import delete, select

from . import profiles
from .models import check_state, device, device_address

PING_TIMEOUT = 2.0
HTTP_TIMEOUT = 10.0
TCP_PORTS = (22, 80, 443, 8006)
CERT_WARN_DAYS = 14
CONCURRENCY = 20


@dataclass(frozen=True)
class Spec:
    device_id: int
    kind: str          # ping | http
    target: str


@dataclass
class Result:
    ok: bool
    summary: str
    ms: float | None = None
    cert_expires_at: float | None = None
    warn: bool = False                 # działa, ale certyfikat wkrótce wygasa (do stanu urządzenia w kroku 4)


# ───────────────────────── co sprawdzać ─────────────────────────
def plan(conn):
    devices = conn.execute(select(device.c.id, device.c.app_url, device.c.kind, device.c.profile_id)).mappings().all()
    addrs = {}
    for a in conn.execute(select(device_address).order_by(device_address.c.is_primary.desc(), device_address.c.id)):
        addrs.setdefault(a.device_id, []).append(a)
    specs = []
    for d in devices:
        if not profiles.value(conn, d["id"], "monitor"):
            continue
        own = addrs.get(d["id"], [])
        ip = next((a.value for a in own if a.kind == "ip" and ":" not in a.value), None)
        host = next((a.value for a in own if a.kind == "hostname"), None)
        if ip or host:
            specs.append(Spec(d["id"], "ping", ip or host))
        if d["app_url"]:
            specs.append(Spec(d["id"], "http", d["app_url"]))
    return specs


# ───────────────────────── ping ─────────────────────────
def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def icmp_ping(ip, timeout=PING_TIMEOUT):
    """Echo ICMP bez uprawnień roota. Zwraca czas w ms albo None (brak odpowiedzi).
    PermissionError, gdy system nie pozwala otworzyć takiego gniazda."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    try:
        sock.settimeout(timeout)
        payload = b"denali" + struct.pack("!d", time.time())
        header = struct.pack("!BBHHH", 8, 0, 0, 0, 1)
        packet = struct.pack("!BBHHH", 8, 0, _checksum(header + payload), 0, 1) + payload
        start = time.perf_counter()
        sock.sendto(packet, (ip, 0))
        deadline = start + timeout
        while True:
            sock.settimeout(max(0.01, deadline - time.perf_counter()))
            data = sock.recv(1024)
            if data and data[0] == 0:            # echo reply
                return (time.perf_counter() - start) * 1000
    except socket.timeout:
        return None
    finally:
        sock.close()


def tcp_alive(host, timeout=PING_TIMEOUT):
    """Czy host odpowiada na którymkolwiek z typowych portów. Odmowa połączenia (RST) też znaczy „żyje”."""
    for port in TCP_PORTS:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout / len(TCP_PORTS) * 2):
                return (time.perf_counter() - start) * 1000, port
        except ConnectionRefusedError:
            return (time.perf_counter() - start) * 1000, port
        except OSError as e:
            if e.errno == errno.ECONNREFUSED:
                return (time.perf_counter() - start) * 1000, port
            continue
    return None, None


async def ping(target):
    try:
        ip = target if _is_ipv4(target) else await asyncio.to_thread(socket.gethostbyname, target)
    except OSError:
        return Result(False, f"nie da się ustalić adresu {target}")
    try:
        ms = await asyncio.to_thread(icmp_ping, ip)
        if ms is not None:
            return Result(True, f"ping {ms:.0f} ms", ms)
    except (PermissionError, OSError):
        pass                                     # brak gniazda ICMP — sprawdzamy porty
    ms, port = await asyncio.to_thread(tcp_alive, ip)
    if ms is not None:
        return Result(True, f"odpowiada (port {port}, {ms:.0f} ms)", ms)
    return Result(False, f"{target} nie odpowiada")


def _is_ipv4(s):
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


# ───────────────────────── HTTP i certyfikat ─────────────────────────
def cert_expiry(host, port=443, timeout=5.0):
    """Data wygaśnięcia certyfikatu (znacznik czasu) — bez sprawdzania zaufania, tylko odczyt."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
        der = s.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return cert.not_valid_after_utc.timestamp()


async def http(url, transport=None, now=None):
    """Aplikacja odpowiada, jeśli zwraca cokolwiek poniżej 500 (logowanie 401/403 też znaczy „działa”).
    Certyfikat nie jest tu weryfikowany (w domu bywa własny) — sprawdzamy tylko, czy nie wygasa."""
    now = now or time.time()
    kw = {"transport": transport} if transport else {"verify": False}
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, **kw) as cli:
            r = await cli.get(url, headers={"User-Agent": "Denali-check"})
    except httpx.TimeoutException:
        return Result(False, "nie odpowiada (przekroczony czas)")
    except httpx.HTTPError as e:
        return Result(False, f"brak połączenia ({e.__class__.__name__})")
    ms = (time.perf_counter() - start) * 1000
    if r.status_code >= 500:
        return Result(False, f"błąd serwera {r.status_code}", ms)
    res = Result(True, f"HTTP {r.status_code} · {ms:.0f} ms", ms)
    u = urlsplit(url)
    if u.scheme == "https":
        try:
            res.cert_expires_at = await asyncio.to_thread(cert_expiry, u.hostname, u.port or 443)
        except (OSError, ValueError, ssl.SSLError):
            pass
    if res.cert_expires_at:
        days = (res.cert_expires_at - now) / 86400
        when = datetime.fromtimestamp(res.cert_expires_at, timezone.utc).astimezone().strftime("%d.%m.%Y")
        if days < 0:
            res.ok, res.summary = False, f"certyfikat wygasł {when}"
        elif days < CERT_WARN_DAYS:
            res.warn = True
            res.summary += f" · certyfikat wygasa {when}"
        else:
            res.summary += f" · certyfikat do {when}"
    return res


# ───────────────────────── uruchomienie i zapis ─────────────────────────
async def run_all(specs, transport=None, now=None):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(spec):
        async with sem:
            if spec.kind == "ping":
                return spec, await ping(spec.target)
            return spec, await http(spec.target, transport, now)
    return await asyncio.gather(*(one(s) for s in specs))


def record(conn, results, specs, now=None):
    now = now or time.time()
    current = {(r.device_id, r.kind, r.target): r for r in conn.execute(select(check_state)).mappings()}
    for spec, res in results:
        key = (spec.device_id, spec.kind, spec.target)
        old = current.get(key)
        values = {"ok": res.ok, "summary": res.summary[:300], "ms": res.ms, "cert_expires_at": res.cert_expires_at,
                  "checked_at": now,
                  "first_fail_at": None if res.ok else ((old["first_fail_at"] if old and old["first_fail_at"] else now)),
                  "last_ok_at": now if res.ok else (old["last_ok_at"] if old else None)}
        if old:
            conn.execute(check_state.update().where(check_state.c.device_id == spec.device_id,
                                                    check_state.c.kind == spec.kind,
                                                    check_state.c.target == spec.target).values(**values))
        else:
            conn.execute(check_state.insert().values(device_id=spec.device_id, kind=spec.kind, target=spec.target,
                                                     **values))
    planned = {(s.device_id, s.kind, s.target) for s in specs}
    for key in set(current) - planned:          # adres albo link zmieniony / monitorowanie wyłączone
        conn.execute(delete(check_state).where(check_state.c.device_id == key[0], check_state.c.kind == key[1],
                                               check_state.c.target == key[2]))


async def run(engine, transport=None, now=None):
    with engine.connect() as conn:
        specs = plan(conn)
    results = await run_all(specs, transport, now) if specs else []
    with engine.begin() as conn:
        record(conn, results, specs, now)
    return len(specs)


def for_device(conn, device_id):
    return conn.execute(select(check_state).where(check_state.c.device_id == device_id)
                        .order_by(check_state.c.kind)).mappings().all()
