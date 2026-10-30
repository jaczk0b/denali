"""Wspólna obsługa certyfikatów dla systemów z certyfikatem z własnym podpisem (Proxmox, PBS, TrueNAS).

Wklejony PEM jest czyszczony (zostają tylko bloki BEGIN/END CERTIFICATE) i sprawdzany już przy zapisie.
"""
import re
import ssl

from . import AdapterError

_PEM = re.compile(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----")


def clean_pem(text: str, hint: str = "") -> str:
    """Z wklejonego tekstu wyciąga same bloki certyfikatu (np. bez linii „root@pve:~# cat …” z konsoli)."""
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        return ""
    blocks = _PEM.findall(text)
    if not blocks:
        raise AdapterError("Certyfikat CA nie wygląda poprawnie — wklej całość od -----BEGIN CERTIFICATE----- "
                           f"do -----END CERTIFICATE-----{f' (wynik: {hint})' if hint else ''}.")
    return "\n".join(b.strip() for b in blocks) + "\n"


def ssl_context(ca_pem: str, verify: bool, hint: str = ""):
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    pem = clean_pem(ca_pem, hint)
    try:
        ctx = ssl.create_default_context(cadata=pem) if pem else ssl.create_default_context()
    except (ssl.SSLError, ValueError):
        raise AdapterError(f"Certyfikat CA jest uszkodzony — skopiuj go jeszcze raz w całości"
                           f"{f' ({hint})' if hint else ''}.") from None
    # Certyfikaty Proxmoxa/PBS nie mają rozszerzeń, których Python 3.13+ wymaga w trybie ścisłym (tak samo w HLC).
    # Łańcuch, podpis i nazwa hosta są dalej sprawdzane.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


def validate_pem_field(config: dict, hint: str = "") -> None:
    """Czyści pole ca_pem w konfiguracji i sprawdza, czy da się z niego zbudować zaufanie (błąd → formularz)."""
    if config.get("ca_pem"):
        config["ca_pem"] = clean_pem(config["ca_pem"], hint)
        ssl_context(config["ca_pem"], True, hint)
