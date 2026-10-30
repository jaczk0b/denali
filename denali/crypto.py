"""Szyfrowanie sekretów w bazie (Fernet). Klucz: DENALI_ENCRYPTION_KEY."""
from cryptography.fernet import Fernet, InvalidToken


class Box:
    def __init__(self, key: str):
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            raise ValueError("Nie da się odszyfrować — inny DENALI_ENCRYPTION_KEY niż przy zapisie?") from None
