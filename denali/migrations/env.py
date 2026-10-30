from alembic import context

from denali.db import make_engine
from denali.models import metadata

config = context.config
url = config.get_main_option("sqlalchemy.url")
if not url:  # `alembic revision` z katalogu projektu — adres bazy z .env
    from denali.config import get_settings
    url = get_settings().db_url


def run_offline():
    context.configure(url=url, target_metadata=metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online():
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
