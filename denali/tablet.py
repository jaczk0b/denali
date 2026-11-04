"""Tablet: na ścianie podgląd, w ręku — po PIN-ie — panel administratora (SSH, VNC, akcje).

Zamiast monitora z KVM przy szafie. Zasady:
- tablet łączy się kodem (admin_routes) i ma sesję „kiosk”: tylko podgląd, tylko z jednego adresu IP,
- PIN ustawia administrator w panelu (po potwierdzeniu hasłem i 2FA) — dla jednego, konkretnego tabletu; w bazie
  tylko skrót argon2,
- poprawny PIN odblokowuje tę sesję jako konto administratora, który go ustawił: do UNLOCK_IDLE bez dotyku,
  najwyżej UNLOCK_MAX; potem tablet sam wraca na ścianę, a otwarte konsole się zamykają,
- MAX_FAILS złych prób z rzędu kasuje PIN (trzeba ustawić nowy w panelu) i wysyła powiadomienie,
- każde odblokowanie, blokada i zła próba trafiają do audytu.
"""
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select

from . import auth
from .db import kv_get, kv_set
from .models import app_user, session, setting

UNLOCK_IDLE = 10 * 60
UNLOCK_MAX = 60 * 60
MAX_FAILS = 5
PIN_MIN, PIN_MAX = 6, 12
_ph = PasswordHasher()


class TabletError(ValueError):
    pass


def _pin_key(public_id):
    return f"tablet.pin.{public_id}"


def _unlock_key(public_id):
    return f"tablet.unlock.{public_id}"


def _kv_delete(conn, key):
    conn.execute(delete(setting).where(setting.c.key == key))


def kiosk_exists(conn, public_id):
    return bool(conn.execute(select(session.c.public_id).where(session.c.kind == "kiosk",
                                                               session.c.public_id == public_id)).first())


# ───────────────────────── PIN (ustawia administrator) ─────────────────────────
def set_pin(conn, public_id, pin, admin_login, now=None):
    pin = (pin or "").strip()
    if not pin.isdigit() or not PIN_MIN <= len(pin) <= PIN_MAX:
        raise TabletError(f"PIN: od {PIN_MIN} do {PIN_MAX} cyfr.")
    if len(set(pin)) == 1 or pin in "01234567890123456789" or pin in "98765432109876543210":
        raise TabletError("Ten PIN jest zbyt oczywisty (same jednakowe cyfry albo kolejne).")
    if not kiosk_exists(conn, public_id):
        raise TabletError("Nie ma takiego tabletu.")
    kv_set(conn, _pin_key(public_id), {"hash": _ph.hash(pin), "user": admin_login, "fails": 0,
                                       "set_at": now or time.time()})


def pin_info(conn, public_id):
    p = kv_get(conn, _pin_key(public_id))
    return {"user": p["user"], "set_at": p["set_at"], "fails": p["fails"]} if p else None


def clear_pin(conn, public_id):
    _kv_delete(conn, _pin_key(public_id))
    _kv_delete(conn, _unlock_key(public_id))


# ───────────────────────── odblokowanie (na tablecie) ─────────────────────────
def unlock(conn, sess, pin, ip="", now=None):
    """Sprawdza PIN tej sesji tabletu. Zwraca login administratora; zły PIN → TabletError
    (z atrybutem wiped=True, gdy właśnie skasowano PIN po MAX_FAILS próbach)."""
    now = now or time.time()
    if sess["kind"] != "kiosk":
        raise TabletError("Odblokowanie działa tylko na połączonym tablecie.")
    p = kv_get(conn, _pin_key(sess["public_id"]))
    if not p:
        raise TabletError("Ten tablet nie ma PIN-u. Ustaw go w panelu: menu konta → Tablet na ścianie.")
    try:
        _ph.verify(p["hash"], (pin or "").strip())
    except (VerifyMismatchError, InvalidHashError):
        fails = p["fails"] + 1
        auth.audit(conn, "tablet_pin_failed", user="tablet", ip=ip, target=sess["public_id"], result=f"próba {fails}")
        if fails >= MAX_FAILS:
            clear_pin(conn, sess["public_id"])
            err = TabletError(f"{MAX_FAILS} złych prób — PIN skasowany. Ustaw nowy w panelu.")
            err.wiped = True
            raise err from None
        kv_set(conn, _pin_key(sess["public_id"]), {**p, "fails": fails})
        raise TabletError(f"Zły PIN. Zostało prób: {MAX_FAILS - fails}.") from None
    admin = conn.execute(select(app_user.c.login, app_user.c.role, app_user.c.disabled)
                         .where(app_user.c.login == p["user"])).first()
    if not admin or admin.disabled or admin.role != "admin":
        raise TabletError(f"Konto „{p['user']}”, które ustawiło PIN, nie jest już aktywnym administratorem.")
    kv_set(conn, _pin_key(sess["public_id"]), {**p, "fails": 0})
    kv_set(conn, _unlock_key(sess["public_id"]), {"user": admin.login, "at": now, "last": now})
    auth.audit(conn, "tablet_unlock", user=admin.login, ip=ip, target=sess["public_id"])
    return admin.login


def lock(conn, sess, ip="", reason="zablokowany"):
    if sess.get("kind") == "kiosk" and kv_get(conn, _unlock_key(sess["public_id"])):
        _kv_delete(conn, _unlock_key(sess["public_id"]))
        auth.audit(conn, "tablet_lock", user=sess.get("login", "tablet"), ip=ip, target=sess["public_id"], result=reason)


def resolve(conn, sess, now=None, touch=True):
    """Sesja z uwzględnieniem odblokowania: odblokowany tablet działa jak sesja administratora
    (rola, login, świeże potwierdzenie). Wygasłe odblokowanie znika. touch: dotyk/żądanie przedłuża bezczynność."""
    if not sess or sess["kind"] != "kiosk":
        return sess
    now = now or time.time()
    u = kv_get(conn, _unlock_key(sess["public_id"]))
    if not u:
        return sess
    if now - u["last"] > UNLOCK_IDLE or now - u["at"] > UNLOCK_MAX:
        lock(conn, sess, reason="minął czas")
        return sess
    if touch and now - u["last"] > 15:
        kv_set(conn, _unlock_key(sess["public_id"]), {**u, "last": now})
        u = {**u, "last": now}
    return {**sess, "role": "admin", "login": u["user"], "elevated": True, "reauth_at": now,
            "unlock_until": min(u["last"] + UNLOCK_IDLE, u["at"] + UNLOCK_MAX)}


# ───────────────────────── ustawienia tego tabletu (ciasteczko) ─────────────────────────
PREFS_COOKIE = "denali_tablet"
MODES = ("panel", "ciemny")


def prefs(raw):
    """„panel;22;7” → {"mode", "quiet_from", "quiet_to"}; godziny ciszy dla dźwięku i przyciemnienia w nocy."""
    parts = (raw or "").split(";")
    mode = parts[0] if parts and parts[0] in MODES else "panel"

    def hour(i, default):
        try:
            h = int(parts[i])
            return h if 0 <= h <= 23 else default
        except (IndexError, ValueError):
            return default
    return {"mode": mode, "quiet_from": hour(1, 22), "quiet_to": hour(2, 7)}


def prefs_value(mode, quiet_from, quiet_to):
    p = prefs(f"{mode};{quiet_from};{quiet_to}")
    return f"{p['mode']};{p['quiet_from']};{p['quiet_to']}"
