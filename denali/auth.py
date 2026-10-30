"""Konta, hasła (argon2id), TOTP, kody zapasowe, sesje po stronie serwera, limit prób, audit log.

Bez zależności od warstwy WWW — te same funkcje woła konsola (create-admin, reset-password) i testy.
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, insert, select, update

from .models import app_user, audit_log, login_attempt, session

ROLES = ("admin", "podglad")
MIN_PASSWORD = 12

IDLE_TIMEOUT = 12 * 3600        # zwykła sesja wygasa po 12 h bezczynności…
MAX_AGE = 14 * 86400            # …i najpóźniej po 14 dniach
KIOSK_AGE = 90 * 86400          # tablet na ścianie, tylko z jednego IP
PENDING_AGE = 300               # między hasłem a kodem 2FA
REAUTH_VALID = 300              # ponowne potwierdzenie ważne 5 min

LOGIN_WINDOW = 900
MAX_FAILS_PER_LOGIN = 5
MAX_FAILS_PER_IP = 20

_ph = PasswordHasher()           # argon2id z domyślnymi parametrami biblioteki
_DUMMY_HASH = _ph.hash("denali-wyrownanie-czasu")


class AuthError(ValueError):
    pass


# ───────────────────────── hasła ─────────────────────────
def hash_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Hasło musi mieć co najmniej {MIN_PASSWORD} znaków.")
    return _ph.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    """Zawsze liczy argon2 — także dla nieistniejącego konta, żeby czas odpowiedzi nie zdradzał, czy login istnieje."""
    try:
        _ph.verify(stored or _DUMMY_HASH, password or "")
        return stored is not None
    except (VerificationError, InvalidHashError):
        return False


# ───────────────────────── TOTP (RFC 6238) i kody zapasowe ─────────────────────────
TOTP_STEP = 30


def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_verify(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """Zwraca krok czasu, jeśli kod jest dobry i nowszy niż ostatnio użyty (ochrona przed ponownym użyciem)."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    current = int((now if now is not None else time.time()) // TOTP_STEP)
    for step in (current - 1, current, current + 1):
        if step > (last_step or 0) and hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def totp_uri(secret: str, login: str) -> str:
    return f"otpauth://totp/Denali:{login}?secret={secret}&issuer=Denali"


def _code_hash(code: str) -> str:
    return hashlib.sha256(re.sub(r"[\s-]", "", code or "").lower().encode()).hexdigest()


def new_backup_codes(n: int = 10) -> tuple[list[str], list[str]]:
    plain = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]
    return plain, [_code_hash(c) for c in plain]


# ───────────────────────── konta ─────────────────────────
def get_user(conn, login: str):
    return conn.execute(select(app_user).where(app_user.c.login == (login or "").strip())).mappings().first()


def create_user(conn, login: str, password: str, role: str) -> int:
    login = (login or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", login):
        raise AuthError("Login: 2–64 znaki, litery, cyfry, kropka, myślnik, podkreślenie.")
    if role not in ROLES:
        raise AuthError(f"Rola: {', '.join(ROLES)}.")
    if get_user(conn, login):
        raise AuthError(f"Konto „{login}” już istnieje.")
    now = time.time()
    res = conn.execute(insert(app_user).values(login=login, pw_hash=hash_password(password), role=role,
                                               totp_last_step=0, backup_codes=[], disabled=False,
                                               created_at=now, pw_changed_at=now))
    return res.inserted_primary_key[0]


def set_password(conn, user_id: int, password: str, keep_token: str | None = None) -> None:
    """Nowe hasło unieważnia wszystkie inne sesje tego konta."""
    conn.execute(update(app_user).where(app_user.c.id == user_id)
                 .values(pw_hash=hash_password(password), pw_changed_at=time.time()))
    revoke_all(conn, user_id, except_token=keep_token)


def enable_totp(conn, user_id: int, secret_ct: str, step: int) -> list[str]:
    plain, hashes = new_backup_codes()
    conn.execute(update(app_user).where(app_user.c.id == user_id)
                 .values(totp_ct=secret_ct, totp_pending_ct=None, totp_last_step=step, backup_codes=hashes))
    return plain


def disable_totp(conn, user_id: int) -> None:
    conn.execute(update(app_user).where(app_user.c.id == user_id)
                 .values(totp_ct=None, totp_pending_ct=None, totp_last_step=0, backup_codes=[]))
    revoke_all(conn, user_id)


def check_second_factor(conn, user, code: str, box, now: float | None = None) -> str | None:
    """Kod z aplikacji albo kod zapasowy. Zwraca "totp" / "zapasowy" / None. Zużyty kod nie zadziała drugi raz."""
    secret = box.decrypt(user["totp_ct"])
    step = totp_verify(secret, code, user["totp_last_step"], now)
    if step is not None:
        conn.execute(update(app_user).where(app_user.c.id == user["id"]).values(totp_last_step=step))
        return "totp"
    h = _code_hash(code)
    codes = list(user["backup_codes"] or [])
    if h in codes:
        codes.remove(h)
        conn.execute(update(app_user).where(app_user.c.id == user["id"]).values(backup_codes=codes))
        return "zapasowy"
    return None


# ───────────────────────── sesje ─────────────────────────
def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def create_session(conn, user_id: int, kind: str, ip: str = "", user_agent: str = "", now: float | None = None) -> str:
    now = now if now is not None else time.time()
    token = secrets.token_urlsafe(32)
    age = {"zwykla": MAX_AGE, "kiosk": KIOSK_AGE, "pending": PENDING_AGE}[kind]
    conn.execute(insert(session).values(token_hash=_token_hash(token), public_id=secrets.token_hex(8), user_id=user_id,
                                        kind=kind, csrf=secrets.token_urlsafe(32), ip=ip or "",
                                        user_agent=(user_agent or "")[:300], created_at=now, last_seen_at=now,
                                        expires_at=now + age))
    return token


def load_session(conn, token: str, ip: str = "", now: float | None = None, touch: bool = True):
    """Sesja razem z kontem albo None (wygasła, bezczynna, inne IP kiosku, konto wyłączone).

    touch=False: odczyt bez odnotowania aktywności — odświeżanie na żywo nie może przedłużać bezczynnej sesji."""
    if not token:
        return None
    now = now if now is not None else time.time()
    row = conn.execute(
        select(session, app_user.c.login, app_user.c.role, app_user.c.disabled, app_user.c.totp_ct)
        .join(app_user, app_user.c.id == session.c.user_id)
        .where(session.c.token_hash == _token_hash(token))
    ).mappings().first()
    if not row:
        return None
    expired = now > row["expires_at"] or (row["kind"] == "zwykla" and now - row["last_seen_at"] > IDLE_TIMEOUT)
    if expired:
        conn.execute(delete(session).where(session.c.token_hash == row["token_hash"]))
        return None
    if row["disabled"] or (row["kind"] == "kiosk" and row["ip"] != ip):
        return None
    if touch and now - row["last_seen_at"] > 60:
        conn.execute(update(session).where(session.c.token_hash == row["token_hash"]).values(last_seen_at=now))
    return dict(row)


def revoke(conn, token: str) -> None:
    conn.execute(delete(session).where(session.c.token_hash == _token_hash(token)))


def revoke_public(conn, user_id: int, public_id: str) -> None:
    conn.execute(delete(session).where(session.c.user_id == user_id, session.c.public_id == public_id))


def revoke_all(conn, user_id: int, except_token: str | None = None) -> None:
    stmt = delete(session).where(session.c.user_id == user_id)
    if except_token:
        stmt = stmt.where(session.c.token_hash != _token_hash(except_token))
    conn.execute(stmt)


def mark_reauth(conn, token: str, now: float | None = None) -> None:
    conn.execute(update(session).where(session.c.token_hash == _token_hash(token))
                 .values(reauth_at=now if now is not None else time.time()))


def reauth_fresh(sess: dict, now: float | None = None) -> bool:
    return bool(sess.get("reauth_at")) and (now if now is not None else time.time()) - sess["reauth_at"] < REAUTH_VALID


def user_sessions(conn, user_id: int):
    return conn.execute(select(session).where(session.c.user_id == user_id, session.c.kind != "pending")
                        .order_by(session.c.last_seen_at.desc())).mappings().all()


# ───────────────────────── limit prób ─────────────────────────
def login_blocked(conn, login: str, ip: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    login = (login or "").strip().lower()
    last_ok = conn.execute(select(func.max(login_attempt.c.ts))
                           .where(login_attempt.c.login == login, login_attempt.c.ok.is_(True))).scalar()
    since_login = max(now - LOGIN_WINDOW, last_ok or 0)
    fails_login = conn.execute(select(func.count()).where(login_attempt.c.login == login, login_attempt.c.ok.is_(False),
                                                          login_attempt.c.ts > since_login)).scalar()
    fails_ip = conn.execute(select(func.count()).where(login_attempt.c.ip == ip, login_attempt.c.ok.is_(False),
                                                       login_attempt.c.ts > now - LOGIN_WINDOW)).scalar()
    return fails_login >= MAX_FAILS_PER_LOGIN or fails_ip >= MAX_FAILS_PER_IP


def record_attempt(conn, login: str, ip: str, ok: bool, now: float | None = None) -> None:
    conn.execute(insert(login_attempt).values(login=(login or "").strip().lower()[:64], ip=ip or "",
                                              ts=now if now is not None else time.time(), ok=ok))


# ───────────────────────── audyt ─────────────────────────
def audit(conn, action: str, user: str = "system", ip: str = "", target: str = "", params: dict | None = None,
          result: str = "ok") -> None:
    conn.execute(insert(audit_log).values(ts=time.time(), user=user or "anonim", ip=ip or "", action=action,
                                          target=str(target)[:200], params=params or {}, result=str(result)[:200]))
