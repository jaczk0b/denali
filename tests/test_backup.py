"""Kopia zapasowa i odtworzenie: pełny obieg, ochrona istniejącej bazy, odrzucanie podejrzanych archiwów."""
import io
import tarfile

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from denali import backup, files, inventory as inv
from denali.config import Settings
from denali.db import make_engine
from denali.models import device
from denali.worker import tick


def test_kopia_i_odtworzenie_na_innym_hoscie(app, settings, tmp_path):
    with app.state.engine.begin() as conn:
        did = inv.create_device(conn, "pve", "host")
        files.store(conn, settings.data_dir, did, "faktura.pdf", b"%PDF-1.4 test")
    archive = backup.create(settings, out_dir=tmp_path / "kopie")
    assert archive.name.startswith("denali-") and archive.name.endswith(".tar.gz")

    # „zapasowy serwer”: pusty katalog danych, ten sam klucz szyfrowania
    other = Settings(_env_file=None, data_dir=tmp_path / "nowy", encryption_key=settings.encryption_key)
    assert backup.restore(other, archive) == 1
    engine = make_engine(other.db_url)
    with engine.connect() as conn:
        assert conn.execute(select(device.c.name)).scalars().all() == ["pve"]
        row, path = files.get(conn, other.data_dir, 1)
    engine.dispose()
    assert path.read_bytes() == b"%PDF-1.4 test" and row["name"] == "faktura.pdf"


def test_odtworzenie_nie_kasuje_istniejacej_bazy(app, settings, tmp_path):
    archive = backup.create(settings, out_dir=tmp_path)
    app.state.engine.dispose()
    with pytest.raises(backup.BackupError, match="--force"):
        backup.restore(settings, archive)
    backup.restore(settings, archive, force=True)
    assert list(settings.data_dir.glob("denali.db.przed-odtworzeniem-*"))


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/cron.d/x", "files/../denali.db", "inny.txt"])
def test_podejrzane_archiwum_odrzucone(tmp_path, name):
    bad = tmp_path / "zle.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for n in ("denali.db", name):
            info = tarfile.TarInfo(n)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    s = Settings(_env_file=None, data_dir=tmp_path / "d", encryption_key=Fernet.generate_key().decode())
    with pytest.raises(backup.BackupError, match="nie wygląda"):
        backup.restore(s, bad)
    assert not (tmp_path / "d").exists()


def test_stare_kopie_usuwane(tmp_path):
    for day in range(1, 21):
        (tmp_path / f"denali-202610{day:02d}-030000.tar.gz").write_bytes(b"x")
    (tmp_path / "moja-notatka.tar.gz").write_bytes(b"x")
    removed = backup.prune(tmp_path, keep=14)
    assert len(removed) == 6 and removed[0].name == "denali-20261001-030000.tar.gz"
    assert (tmp_path / "moja-notatka.tar.gz").exists()


def test_worker_robi_kopie_raz_na_dobe(app, settings):
    tick(app.state.engine, settings, {}, now=1_800_000_000)
    tick(app.state.engine, settings, {}, now=1_800_000_000 + 3600)
    assert len(list(backup.backups_dir(settings.data_dir).glob("denali-*.tar.gz"))) == 1
    tick(app.state.engine, settings, {}, now=1_800_000_000 + 86400)
    assert len(list(backup.backups_dir(settings.data_dir).glob("denali-*.tar.gz"))) == 2
