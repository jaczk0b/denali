"""Skarbiec haseł. Tylko administrator; odsłonięcie wymaga świeżego potwierdzenia hasłem i trafia do audytu.

Sprawdzenia roli i potwierdzenia są tutaj, a nie tylko w widoku — żeby żadna droga w kodzie ich nie ominęła.
"""
import time

from sqlalchemy import delete, select

from . import auth
from .models import device, vault_secret


class VaultError(PermissionError):
    pass


def _check_admin(role):
    if role != "admin":
        raise VaultError("Skarbiec jest tylko dla administratora.")


def add(conn, box, label, secret, *, role, user, ip="", device_id=None, username="", note="", now=None):
    _check_admin(role)
    label = (label or "").strip()
    if not label or not secret:
        raise ValueError("Podaj nazwę i hasło.")
    if device_id is not None and not conn.execute(select(device.c.id).where(device.c.id == device_id)).first():
        raise ValueError("Nie ma takiego urządzenia.")
    sid = conn.execute(vault_secret.insert().values(
        device_id=device_id, label=label[:200], username=(username or "").strip()[:200],
        ciphertext=box.encrypt(secret), note=(note or "").strip()[:500], updated_at=now or time.time(),
        updated_by=user)).inserted_primary_key[0]
    auth.audit(conn, "secret_create", user=user, ip=ip, target=label)
    return sid


def update(conn, box, secret_id, *, role, user, ip="", label=None, secret=None, username=None, note=None, now=None):
    _check_admin(role)
    row = _get(conn, secret_id)
    values = {"updated_at": now or time.time(), "updated_by": user}
    if label is not None:
        if not label.strip():
            raise ValueError("Podaj nazwę.")
        values["label"] = label.strip()[:200]
    if secret:
        values["ciphertext"] = box.encrypt(secret)
    if username is not None:
        values["username"] = username.strip()[:200]
    if note is not None:
        values["note"] = note.strip()[:500]
    conn.execute(vault_secret.update().where(vault_secret.c.id == secret_id).values(**values))
    auth.audit(conn, "secret_update", user=user, ip=ip, target=row["label"], params={"haslo_zmienione": bool(secret)})


def remove(conn, secret_id, *, role, user, ip=""):
    _check_admin(role)
    row = _get(conn, secret_id)
    conn.execute(delete(vault_secret).where(vault_secret.c.id == secret_id))
    auth.audit(conn, "secret_delete", user=user, ip=ip, target=row["label"])


def list_secrets(conn, *, role, device_id=None):
    """Lista bez haseł."""
    _check_admin(role)
    q = select(vault_secret.c.id, vault_secret.c.device_id, vault_secret.c.label, vault_secret.c.username,
               vault_secret.c.note, vault_secret.c.updated_at, vault_secret.c.updated_by).order_by(vault_secret.c.label)
    if device_id is not None:
        q = q.where(vault_secret.c.device_id == device_id)
    return conn.execute(q).mappings().all()


def reveal(conn, box, secret_id, *, role, reauth_at, user, ip="", now=None):
    """Zwraca hasło. Wymaga roli admin i potwierdzenia hasłem w ostatnich auth.REAUTH_VALID sekundach."""
    row = _get(conn, secret_id)
    if role != "admin":
        auth.audit(conn, "secret_reveal", user=user, ip=ip, target=row["label"], result="odmowa: rola")
        raise VaultError("Skarbiec jest tylko dla administratora.")
    if not auth.reauth_fresh({"reauth_at": reauth_at}, now):
        raise VaultError("Potwierdź hasłem (i kodem 2FA), żeby odsłonić hasło.")
    auth.audit(conn, "secret_reveal", user=user, ip=ip, target=row["label"])
    return box.decrypt(row["ciphertext"])


def _get(conn, secret_id):
    row = conn.execute(select(vault_secret).where(vault_secret.c.id == secret_id)).mappings().first()
    if not row:
        raise ValueError("Nie ma takiego wpisu w skarbcu.")
    return row
