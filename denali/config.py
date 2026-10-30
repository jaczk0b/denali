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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "denali.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    def check(self) -> None:
        """Bez klucza szyfrowania aplikacja nie wstaje (poza trybem deweloperskim)."""
        if not self.encryption_key and not self.dev:
            raise SystemExit("Brak DENALI_ENCRYPTION_KEY w .env. Wygeneruj: python -m denali genkeys >> .env")


@lru_cache
def get_settings() -> Settings:
    return Settings()
