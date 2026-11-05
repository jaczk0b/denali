"""Udostępnianie: usługi przydzielane kontom z rolą „uzytkownik” (własny pulpit, status, instrukcje).

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TAILSCALE = """Tailscale to bezpieczne połączenie z domową siecią. Bez niego część usług nie otworzy się spoza domu.

1. Zainstaluj aplikację: [tailscale.com/download](https://tailscale.com/download) (Windows, macOS, Android, iPhone).
2. Otwórz link z zaproszeniem i zaloguj się tym samym kontem w aplikacji.
3. Włącz połączenie w aplikacji i poczekaj, aż pokaże *Connected*.
4. Wróć na tę stronę i otwórz wybraną usługę.

**Na Linuksie** po zalogowaniu uruchom jeszcze `sudo tailscale up --accept-routes`.

**Nie działa?** Wyłącz i włącz Tailscale, a potem sprawdź stronę *Status*: może to przerwa po stronie serwera.
"""


def upgrade():
    op.create_table(
        "share",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target", sa.String(500), nullable=False, server_default=""),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="SET NULL")),
        sa.Column("body_md", sa.Text, nullable=False, server_default=""),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "share_user",
        sa.Column("share_id", sa.Integer, sa.ForeignKey("share.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True),
    )
    op.get_bind().execute(sa.text("INSERT INTO share (title, kind, body_md, position) VALUES (:t, 'instrukcja', :b, 1)"),
                          {"t": "Tailscale", "b": TAILSCALE})


def downgrade():
    op.execute("DELETE FROM app_user WHERE role = 'uzytkownik'")
    op.drop_table("share_user")
    op.drop_table("share")
