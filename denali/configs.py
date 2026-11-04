"""Kopie konfiguracji urządzeń (na razie OpenWrt) — zapis wyniku akcji „Kopia konfiguracji”.

Archiwum zawiera hasła Wi-Fi i skróty haseł kont, więc: zaszyfrowane w bazie (tym samym kluczem co skarbiec, trafia
do kopii zapasowej panelu razem z bazą), lista tylko dla admina, pobranie po świeżym potwierdzeniu i z wpisem w audycie.
"""
import base64
import hashlib
import re
import time

from sqlalchemy import delete, select

from . import auth
from .models import config_backup

KEEP = 10                       # tyle różnych kopii na urządzenie; starsze znikają


def store(conn, box, device_id, name, data, source="", user="", now=None):
    """Zapisuje kopię. Ta sama treść co ostatnia → tylko nowa data sprawdzenia. Zwraca (id, czy_nowa)."""
    now = now or time.time()
    digest = hashlib.sha256(data).hexdigest()
    last = conn.execute(select(config_backup.c.id, config_backup.c.sha256).where(
        config_backup.c.device_id == device_id).order_by(config_backup.c.taken_at.desc()).limit(1)).first()
    if last and last.sha256 == digest:
        conn.execute(config_backup.update().where(config_backup.c.id == last.id).values(checked_at=now))
        return last.id, False
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name or "kopia.tar.gz")[:150]
    bid = conn.execute(config_backup.insert().values(
        device_id=device_id, source=str(source)[:100], name=name, taken_at=now, checked_at=now, size=len(data),
        sha256=digest, data_ct=box.encrypt(base64.b64encode(data).decode()), taken_by=str(user)[:64])
    ).inserted_primary_key[0]
    old = [r.id for r in conn.execute(select(config_backup.c.id).where(config_backup.c.device_id == device_id)
                                      .order_by(config_backup.c.taken_at.desc()).offset(KEEP))]
    if old:
        conn.execute(delete(config_backup).where(config_backup.c.id.in_(old)))
    return bid, True


def for_device(conn, device_id):
    """Opis kopii (bez treści), od najnowszej."""
    cols = [c for c in config_backup.c if c.name != "data_ct"]
    return conn.execute(select(*cols).where(config_backup.c.device_id == device_id)
                        .order_by(config_backup.c.taken_at.desc())).mappings().all()


def download(conn, box, device_id, backup_id, *, role, reauth_fresh, user, ip=""):
    """(nazwa pliku, treść). Tylko admin po świeżym potwierdzeniu; każde pobranie w audycie."""
    if role != "admin" or not reauth_fresh:
        raise PermissionError("Pobranie kopii konfiguracji wymaga potwierdzenia hasłem i kodem.")
    row = conn.execute(select(config_backup).where(config_backup.c.id == backup_id,
                                                   config_backup.c.device_id == device_id)).mappings().first()
    if not row:
        raise LookupError("Nie ma takiej kopii.")
    data = base64.b64decode(box.decrypt(row["data_ct"]))
    auth.audit(conn, "config_backup_download", user=user, ip=ip, target=row["name"],
               params={"device_id": device_id, "backup": backup_id, "sha256": row["sha256"]})
    stem = row["name"].removesuffix(".tar.gz")
    return f"{stem}-{time.strftime('%Y-%m-%d-%H%M', time.localtime(row['taken_at']))}.tar.gz", data
