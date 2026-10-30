"""Migracje schematu (Alembic) uruchamiane z kodu: `python -m denali migrate` przy każdym starcie kontenera."""
from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(settings: Settings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    return cfg


def upgrade(settings: Settings) -> None:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".zapis-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise SystemExit(f"Nie mogę pisać do katalogu danych {settings.data_dir} ({e.strerror}). "
                         f"Katalog na hoście (DENALI_DATA_PATH) musi należeć do UID 1000: "
                         f"chown -R 1000:1000 <katalog>") from None
    command.upgrade(alembic_config(settings), "head")
