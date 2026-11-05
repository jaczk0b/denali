"""Konfiguracja z zmiennych środowiskowych / pliku .env (prefiks DENALI_)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DENALI_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    encryption_key: str = ""
    base_url: str = "https://denali.serwispowers.com"
    # Adresy, którym wierzymy w nagłówkach X-Forwarded-* — tylko reverse proxy (Nginx Proxy Manager).
    # Nigdy "*": port panelu jest otwarty w LAN, więc każdy mógłby podać fałszywy adres klienta.
    trusted_proxies: list[str] = ["127.0.0.1"]
    dev: bool = False
    # /metrics i /api/stan (Prometheus, Home Assistant): nagłówek „Authorization: Bearer …”. Puste = wyłączone.
    metrics_token: str = ""
    # Logowanie przez OIDC (Pocket ID). Puste issuer albo client_id = wyłączone; własne konto zostaje zawsze.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_label: str = "Zaloguj przez Pocket ID"
    # Grupy u dostawcy → rola w Denali. Wszystkie puste: wchodzą tylko konta połączone ręcznie (Konto → Połącz).
    oidc_admin_group: str = ""
    oidc_viewer_group: str = ""
    oidc_user_group: str = ""             # konta widzące tylko przydzielone usługi

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "denali.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    def check(self) -> None:
        """Bez poprawnego klucza szyfrowania aplikacja nie wstaje (poza trybem deweloperskim bez klucza)."""
        if not self.encryption_key:
            if not self.dev:
                raise SystemExit("Brak DENALI_ENCRYPTION_KEY w .env. Wygeneruj: python -m denali genkeys >> .env")
            return
        from cryptography.fernet import Fernet
        try:
            Fernet(self.encryption_key)
        except ValueError:
            raise SystemExit("DENALI_ENCRYPTION_KEY to nie jest klucz szyfrowania, tylko zwykły tekst. Klucz to 44 losowe "
                             "znaki — wygeneruj go: python -m denali genkeys (w Dockerze: docker run --rm denali:local "
                             "python -m denali genkeys)") from None


@lru_cache
def get_settings() -> Settings:
    return Settings()
