import pytest
from sqlalchemy import select

from denali import auth, inventory as inv, vault
from denali.models import audit_log, vault_secret


@pytest.fixture
def box(app):
    return app.state.box


def audit_rows(conn):
    return conn.execute(select(audit_log.c.action, audit_log.c.result)).all()


def test_haslo_zaszyfrowane_w_bazie(conn, box):
    d = inv.create_device(conn, "router", "router")
    sid = vault.add(conn, box, "Router admin", "Tajne-Haslo-123", role="admin", user="jakub", device_id=d,
                    username="root")
    ct = conn.execute(select(vault_secret.c.ciphertext).where(vault_secret.c.id == sid)).scalar_one()
    assert "Tajne-Haslo-123" not in ct
    listed = vault.list_secrets(conn, role="admin", device_id=d)
    assert listed[0]["label"] == "Router admin" and "ciphertext" not in listed[0]


def test_odsloniecie_wymaga_swiezego_potwierdzenia(conn, box):
    sid = vault.add(conn, box, "NAS", "haslo-nas", role="admin", user="jakub")
    with pytest.raises(vault.VaultError, match="Potwierdź"):
        vault.reveal(conn, box, sid, role="admin", reauth_at=None, user="jakub")
    with pytest.raises(vault.VaultError, match="Potwierdź"):
        vault.reveal(conn, box, sid, role="admin", reauth_at=1000.0, user="jakub",
                     now=1000.0 + auth.REAUTH_VALID + 1)
    assert vault.reveal(conn, box, sid, role="admin", reauth_at=1000.0, user="jakub", now=1010.0) == "haslo-nas"
    assert ("secret_reveal", "ok") in audit_rows(conn)


def test_podglad_nie_ma_dostepu(conn, box):
    sid = vault.add(conn, box, "NAS", "haslo-nas", role="admin", user="jakub")
    with pytest.raises(vault.VaultError):
        vault.reveal(conn, box, sid, role="podglad", reauth_at=1000.0, user="widz", now=1001.0)
    with pytest.raises(vault.VaultError):
        vault.list_secrets(conn, role="podglad")
    with pytest.raises(vault.VaultError):
        vault.add(conn, box, "X", "y", role="podglad", user="widz")
    assert ("secret_reveal", "odmowa: rola") in audit_rows(conn)


def test_zmiana_i_usuniecie(conn, box):
    sid = vault.add(conn, box, "NAS", "stare", role="admin", user="jakub")
    vault.update(conn, box, sid, role="admin", user="jakub", secret="nowe", note="po wymianie")
    assert vault.reveal(conn, box, sid, role="admin", reauth_at=1.0, user="jakub", now=2.0) == "nowe"
    vault.remove(conn, sid, role="admin", user="jakub")
    assert vault.list_secrets(conn, role="admin") == []
    assert [a for a, _ in audit_rows(conn)] == ["secret_create", "secret_update", "secret_reveal", "secret_delete"]
