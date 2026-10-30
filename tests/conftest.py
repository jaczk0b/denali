import re
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from denali import auth
from denali.app import create_app
from denali.config import Settings
from denali.migrate import upgrade
from denali.models import app_user

ADMIN_PW = "bardzo-dlugie-haslo-admina"


@pytest.fixture
def settings(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, encryption_key=Fernet.generate_key().decode(), dev=False)
    upgrade(s)
    return s


@pytest.fixture
def app(settings):
    a = create_app(settings)
    yield a
    a.state.engine.dispose()


def new_client(app) -> TestClient:
    # https, bo ciasteczka sesji mają flagę Secure
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture
def client(app):
    with new_client(app) as c:
        yield c


def make_user(app, login="admin", password=ADMIN_PW, role="admin"):
    with app.state.engine.begin() as conn:
        return auth.create_user(conn, login, password, role)


def user_row(app, login="admin"):
    with app.state.engine.connect() as conn:
        return conn.execute(select(app_user).where(app_user.c.login == login)).mappings().first()


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def do_login(client, login="admin", password=ADMIN_PW):
    client.get("/login")
    return client.post("/login", data={"login": login, "password": password,
                                       "pre": client.cookies.get("denali_prelogin"), "next": ""})


def enable_2fa(client, app, login="admin") -> str:
    """Włącza 2FA przez interfejs, zwraca sekret."""
    page = client.get("/konto/2fa").text
    secret = re.search(r'Wpisz ręcznie: <span class="mono">([A-Z2-7]+)</span>', page).group(1)
    code = auth.totp_code(secret, int(time.time() // 30))
    r = client.post("/konto/2fa", data={"csrf_token": csrf_from(page), "code": code})
    assert r.status_code == 200 and "Kody zapasowe" in r.text
    return secret


@pytest.fixture
def conn(app):
    """Połączenie w transakcji — do testów logiki na prawdziwej bazie po migracjach."""
    with app.state.engine.begin() as c:
        yield c
