"""Kopia zapasowa i odtwarzanie: jedno archiwum z bazą (spójna kopia przez API SQLite) i plikami urządzeń.

Klucza szyfrowania (DENALI_ENCRYPTION_KEY) celowo NIE ma w archiwum — trzymasz go w menedżerze haseł.
Bez niego skarbiec i 2FA z kopii są nie do odczytania; reszta (urządzenia, procedury, incydenty) działa.
Eksportu awaryjnego też nie ma — worker odtworzy go sam.
"""
import io
import re
import shutil
import sqlite3
import tarfile
import time
from datetime import datetime
from pathlib import Path

KEEP = 14                   # ile kopii trzymać w data/backups
_NAME = re.compile(r"^denali-\d{8}-\d{6}\.tar\.gz$")
_MEMBER = re.compile(r"^(denali\.db|files/[0-9a-f]{32})$")


class BackupError(Exception):
    pass


def backups_dir(data_dir):
    return Path(data_dir) / "backups"


def create(settings, out_dir=None, now=None):
    """Tworzy archiwum. Działa przy pracującym panelu (kopia bazy przez API SQLite jest spójna)."""
    data_dir = Path(settings.data_dir)
    out_dir = Path(out_dir) if out_dir else backups_dir(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"denali-{stamp}.tar.gz"
    part = target.with_suffix(".part")
    db_copy = out_dir / f".denali-{stamp}.db"
    try:
        src = sqlite3.connect(settings.db_path)
        dst = sqlite3.connect(db_copy)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with tarfile.open(part, "w:gz") as tar:
            tar.add(db_copy, arcname="denali.db")
            files = data_dir / "files"
            if files.is_dir():
                for f in sorted(files.iterdir()):
                    if f.is_file() and _MEMBER.match(f"files/{f.name}"):
                        tar.add(f, arcname=f"files/{f.name}")
        part.replace(target)
    finally:
        db_copy.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return target


def prune(out_dir, keep=KEEP):
    """Zostawia `keep` najnowszych archiwów. Zwraca usunięte."""
    archives = sorted(p for p in Path(out_dir).glob("denali-*.tar.gz") if _NAME.match(p.name))
    removed = archives[:-keep] if keep else archives
    for p in removed:
        p.unlink()
    return removed


def restore(settings, archive, force=False):
    """Odtwarza bazę i pliki z archiwum. Panel i worker muszą być zatrzymane.

    Istniejąca baza nie jest kasowana: bez force odmowa, z force zostaje odłożona obok jako kopia.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise BackupError(f"Nie ma pliku {archive}")
    data_dir = Path(settings.data_dir)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        bad = [n for n, m in zip(names, members) if not _MEMBER.match(n) or not m.isfile()]
        if bad or "denali.db" not in names:
            raise BackupError("To nie wygląda na archiwum Denali (nieoczekiwana zawartość: "
                              f"{', '.join(bad[:3]) or 'brak denali.db'}).")
        db = Path(settings.db_path)
        if db.exists():
            if not force:
                raise BackupError(f"{db} już istnieje. Zatrzymaj panel i dodaj --force (obecna baza zostanie "
                                  "odłożona obok, nie skasowana).")
            aside = db.with_name(f"denali.db.przed-odtworzeniem-{datetime.now():%Y%m%d-%H%M%S}")
            db.replace(aside)
            for suffix in ("-wal", "-shm"):
                Path(f"{db}{suffix}").unlink(missing_ok=True)
        (data_dir / "files").mkdir(parents=True, exist_ok=True)
        for m in members:
            src = tar.extractfile(m)
            dest = data_dir / m.name          # nazwa sprawdzona wyrażeniem _MEMBER — bez „..” i ścieżek bezwzględnych
            with open(dest, "wb") as out:
                shutil.copyfileobj(src or io.BytesIO(), out)
    from .migrate import upgrade
    upgrade(settings)                          # kopia ze starszej wersji → schemat do bieżącej
    return len(members) - 1
