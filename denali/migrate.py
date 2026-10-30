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
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")
