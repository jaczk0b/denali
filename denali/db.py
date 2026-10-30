"""SQLite w trybie WAL przez SQLAlchemy Core. Cały stan panelu w jednym pliku — łatwy backup i restore."""
import time

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.dialects.sqlite import insert

from .models import setting


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def kv_get(conn, key: str, default=None):
    row = conn.execute(select(setting.c.value).where(setting.c.key == key)).first()
    return row[0] if row else default


def kv_set(conn, key: str, value) -> None:
    stmt = insert(setting).values(key=key, value=value, updated_at=time.time())
    conn.execute(stmt.on_conflict_do_update(index_elements=[setting.c.key],
                                            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}))
