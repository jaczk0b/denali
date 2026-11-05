"""Udostępnianie usług: co konto z rolą „uzytkownik” widzi na swoim pulpicie i na stronie statusu.

Do takiego konta trafiają tylko tytuły, adresy i instrukcje wpisane tutaj oraz ogólny stan usługi —
nigdy nazwy urządzeń, adresy z inwentarza ani powody awarii.
"""
import re
import secrets
import time

from sqlalchemy import delete, func, insert, select, update

from . import auth, live, maintenance, status
from .models import app_user, share, share_user

ROLE = auth.USER_ROLE
KINDS = {"link": "Aplikacja (link)", "adres": "Adres do skopiowania (np. SMB)", "instrukcja": "Instrukcja",
         "status": "Tylko na stronie statusu"}
_WORST = {"down": 0, "warn": 1, "unknown": 2, "ok": 3}


class ShareError(ValueError):
    pass


# ───────────────────────── usługi ─────────────────────────
def get(conn, share_id):
    return conn.execute(select(share).where(share.c.id == share_id)).mappings().first()


def all_shares(conn):
    return conn.execute(select(share).order_by(share.c.position, share.c.id)).mappings().all()


def for_session(conn, sess):
    """Administrator widzi wszystko (podgląd strony), reszta tylko to, co jej przydzielono."""
    if sess["role"] == "admin":
        return all_shares(conn)
    return conn.execute(select(share).join(share_user, share_user.c.share_id == share.c.id)
                        .where(share_user.c.user_id == sess["user_id"])
                        .order_by(share.c.position, share.c.id)).mappings().all()


def _clean(title, kind, target, device_id, body_md):
    title, target, body_md = (title or "").strip(), (target or "").strip(), (body_md or "").strip()
    if not title or len(title) > 200:
        raise ShareError("Podaj nazwę (do 200 znaków).")
    if kind not in KINDS:
        raise ShareError("Nieznany rodzaj usługi.")
    if len(target) > 500 or re.search(r"[\x00-\x1f]", target):
        raise ShareError("Zły adres.")
    if kind == "link" and not re.fullmatch(r"https?://[^\s/]\S*", target, re.IGNORECASE):
        raise ShareError("Link musi zaczynać się od http:// albo https://")
    if kind == "adres" and not target:
        raise ShareError("Podaj adres, np. \\\\nas\\zdjecia")
    if kind == "instrukcja" and not body_md:
        raise ShareError("Instrukcja nie może być pusta.")
    if kind == "status" and device_id is None:
        raise ShareError("Wybierz urządzenie, którego stan pokazać.")
    if kind in ("instrukcja", "status"):
        target = ""
    return {"title": title, "kind": kind, "target": target, "device_id": device_id, "body_md": body_md}


def create(conn, title, kind, target="", device_id=None, body_md=""):
    values = _clean(title, kind, target, device_id, body_md)
    position = (conn.execute(select(func.max(share.c.position))).scalar() or 0) + 1
    return conn.execute(insert(share).values(**values, position=position)).inserted_primary_key[0]


def edit(conn, share_id, title, kind, target="", device_id=None, body_md=""):
    conn.execute(update(share).where(share.c.id == share_id).values(**_clean(title, kind, target, device_id, body_md)))


def remove(conn, share_id):
    conn.execute(delete(share).where(share.c.id == share_id))


# ───────────────────────── stan w prostych słowach ─────────────────────────
def _state(device_id, states, marks, breaks):
    if device_id is None:
        return None
    if device_id in breaks:
        until = time.strftime("%H:%M", time.localtime(breaks[device_id]["ends_at"]))
        return {"state": "warn", "text": f"Planowana przerwa do {until}"}
    mark = marks.get(device_id)
    if mark in ("przyczyna", "skutek"):
        return {"state": "down", "text": "Nie działa"}
    if mark == "gorzej":
        return {"state": "warn", "text": "Działa wolniej lub z przerwami"}
    return {"ok": {"state": "ok", "text": "Działa"},
            "warn": {"state": "warn", "text": "Działa wolniej lub z przerwami"},
            "down": {"state": "down", "text": "Nie działa"},
            "off": {"state": "down", "text": "Wyłączone"},
            }.get((states.get(device_id) or {}).get("state"), {"state": "unknown", "text": "Brak danych"})


def with_status(conn, rows):
    states, marks, breaks = live.saved(conn), status.device_marks(conn), maintenance.active(conn)
    return [{**r, "status": _state(r["device_id"], states, marks, breaks)} for r in rows]


def summary(items):
    """Jedno zdanie na górę strony statusu."""
    known = [i["status"]["state"] for i in items if i["status"]]
    if not known:
        return {"state": "unknown", "text": "Nie ma tu jeszcze nic do pokazania"}
    worst = min(known, key=_WORST.get)
    return {"state": worst, "text": {"ok": "Wszystko działa", "warn": "Działa, ale nie wszystko idzie gładko",
                                     "down": "Coś nie działa", "unknown": "Nie mam teraz danych o stanie"}[worst]}


# ───────────────────────── konta ─────────────────────────
def accounts(conn):
    users = conn.execute(select(app_user).where(app_user.c.role == ROLE).order_by(app_user.c.login)).mappings().all()
    links = conn.execute(select(share_user)).all()
    return [{**u, "has_password": auth.has_password(u), "share_ids": {l.share_id for l in links if l.user_id == u["id"]}}
            for u in users]


def get_account(conn, user_id):
    """Tylko konta z tej roli: tą drogą nie da się dotknąć konta administratora."""
    return conn.execute(select(app_user).where(app_user.c.id == user_id, app_user.c.role == ROLE)).mappings().first()


def new_password():
    return secrets.token_urlsafe(12)


def create_account(conn, login):
    password = new_password()
    return auth.create_user(conn, login, password, ROLE), password


def assign(conn, user_id, share_ids):
    known = {r["id"] for r in all_shares(conn)}
    conn.execute(delete(share_user).where(share_user.c.user_id == user_id))
    for sid in set(share_ids) & known:
        conn.execute(insert(share_user).values(share_id=sid, user_id=user_id))


def set_disabled(conn, user_id, disabled):
    conn.execute(update(app_user).where(app_user.c.id == user_id).values(disabled=disabled))
    if disabled:
        auth.revoke_all(conn, user_id)
