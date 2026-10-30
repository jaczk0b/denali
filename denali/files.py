"""Pliki przy urządzeniach (zdjęcia, instrukcje, faktury). Na dysku pod losową nazwą, nigdy pod nazwą od użytkownika."""
import hashlib
import re
import secrets
import time
from pathlib import Path

from sqlalchemy import delete, select

from .models import device, file

MAX_SIZE = 25 * 1024 * 1024
_MIME = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


class FileError(ValueError):
    pass


def files_dir(data_dir):
    return Path(data_dir) / "files"


def clean_name(name):
    name = re.split(r"[\\/]", name or "")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip(" .")
    return name[:200] or "plik"


def store(conn, data_dir, device_id, name, data, mime="application/octet-stream", user="", now=None):
    if not conn.execute(select(device.c.id).where(device.c.id == device_id)).first():
        raise FileError("Nie ma takiego urządzenia.")
    if not data:
        raise FileError("Plik jest pusty.")
    if len(data) > MAX_SIZE:
        raise FileError(f"Plik jest za duży (limit {MAX_SIZE // 1024 // 1024} MB).")
    mime = (mime or "").lower().split(";")[0].strip()
    if not _MIME.match(mime):
        mime = "application/octet-stream"
    path = secrets.token_hex(16)
    target = files_dir(data_dir) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return conn.execute(file.insert().values(device_id=device_id, name=clean_name(name), mime=mime, size=len(data),
                                             sha256=hashlib.sha256(data).hexdigest(), path=path,
                                             uploaded_at=now or time.time(), uploaded_by=user)).inserted_primary_key[0]


def get(conn, data_dir, file_id):
    """(opis, ścieżka na dysku) albo None."""
    row = conn.execute(select(file).where(file.c.id == file_id)).mappings().first()
    if not row:
        return None
    return row, files_dir(data_dir) / row["path"]


def for_device(conn, device_id):
    return conn.execute(select(file).where(file.c.device_id == device_id).order_by(file.c.name)).mappings().all()


def remove(conn, data_dir, file_id):
    found = get(conn, data_dir, file_id)
    if found:
        conn.execute(delete(file).where(file.c.id == file_id))
        found[1].unlink(missing_ok=True)


def cleanup_orphans(conn, data_dir):
    """Usuwa z dysku pliki, których nie ma już w bazie (np. po usunięciu urządzenia). Zwraca ich liczbę."""
    d = files_dir(data_dir)
    if not d.exists():
        return 0
    known = {r.path for r in conn.execute(select(file.c.path))}
    removed = 0
    for p in d.iterdir():
        if p.is_file() and p.name not in known:
            p.unlink()
            removed += 1
    return removed
