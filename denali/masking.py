"""Maskowanie sekretów w tekście (eksport Markdown). Przeniesione z HLC.

Dwie warstwy: sekrety podane dosłownie (argument `extra`) i wzorce (KEY=..., Bearer ..., user:pass@host, klucze
prywatne, JWT, tokeny znanych usług). Lepiej zamaskować za dużo niż za mało.
"""
import re

MASK = "«ukryte»"

_SECRET_WORDS = r"(?:PASS(?:WORD|WD)?|SECRET|TOKEN|API[_-]?KEY|APIKEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|CREDENTIALS?|AUTH|PWD|SALT|DSN|COOKIE|SESSION[_-]?KEY|ENCRYPTION[_-]?KEY|JWT)"
# do fullmatch na nazwie klucza (krótki napis — bez limitu)
_SECRET_KEY_NAMES = r"(?:[A-Za-z0-9_.-]*" + _SECRET_WORDS + r"[A-Za-z0-9_.-]*)"
# do wyszukiwania w długim tekście: kotwica na początku słowa + ograniczona długość. Bez tego wzorzec
# z wiodącym `[...]*` ma złożoność kwadratową — jedna długa linia logu zatrzymywała maskowanie na kilkanaście sekund.
_SECRET_KEY_SCAN = r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]{0,48}?" + _SECRET_WORDS + r"[A-Za-z0-9_.-]{0,48})"

PATTERNS = [
    # bloki kluczy prywatnych
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    # nagłówki autoryzacji — przed KEY=value, żeby „Authorization: Bearer x” nie zostawił samego tokena
    (re.compile(r"(?i)\b(Bearer|Basic|Token|PVEAPIToken)[ =]+([A-Za-z0-9._~+/=:@!\-]{8,})"), lambda m: m.group(1) + " " + MASK),
    # KEY=value / KEY: value / "key": "value" (env, yaml, json)
    (re.compile(r"(?i)(\"?" + _SECRET_KEY_SCAN + r"\"?[ \t]*[:=][ \t]*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}\n]+)"),
     lambda m: m.group(1) + (m.group(2)[0] + MASK + m.group(2)[0] if m.group(2)[:1] in "\"'" else MASK)),
    # proza: „hasło SuperTajne123”, „hasło to: x”, „password is x”, „token: x” (słowo ≥ 4 znaki po nim)
    (re.compile(r"(?i)\b(has[łl][oa]|password|passwd|pass|pwd|token|sekret|secret|pin)\b(\s*(?:to|jest|is|was|:|=|-)?\s*[:=]?\s*)"
                r"(?!«)(?!(?:do|dla|od|na|w|z|jest|to|the|for|of|is)\b)([^\s,;«]{4,})"), lambda m: m.group(1) + m.group(2) + MASK),
    # --password xyz / -p xyz przy znanych narzędziach
    (re.compile(r"(?i)(--?(?:password|passwd|token|secret|api-key)[= ])(\S+)"), lambda m: m.group(1) + MASK),
    # user:pass@host w URL
    (re.compile(r"((?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,20}://[^/\s:@]{1,200}:)([^@\s/]{1,300})(@)", re.I), lambda m: m.group(1) + MASK + m.group(3)),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),
    # znane formaty tokenów
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), MASK),                    # Anthropic
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),                  # GitHub
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), MASK),                    # GitLab
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), MASK),                 # Slack
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),                          # AWS
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), MASK),                # Telegram bot
    (re.compile(r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+"), lambda m: m.group(1) + MASK),
    (re.compile(r"\bhlca_[A-Za-z0-9_-]{16,}"), MASK),                     # tokeny agentów HLC
    (re.compile(r"\bhlci_[A-Za-z0-9_-]{8,}"), MASK),                      # kody instalacyjne HLC
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b(?=.*(?:secret|token))", re.I), MASK),
]


def mask(text, extra=()) -> str:
    """Maskuje sekrety w tekście. Bezpieczne dla None i nie-stringów."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for v in sorted((e for e in extra if e), key=len, reverse=True):
        if v in text:
            text = text.replace(v, MASK)
    for rx, repl in PATTERNS:
        text = rx.sub(repl, text)
    return text


def mask_obj(obj, extra=()):
    """Rekurencyjnie maskuje stringi w strukturze (dict/list). Klucze o sekretnej nazwie → całe wartości."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and re.fullmatch(_SECRET_KEY_NAMES, k, re.I) and isinstance(v, (str, int, float)) and v != "":
                out[k] = MASK
            else:
                out[k] = mask_obj(v, extra)
        return out
    if isinstance(obj, list):
        return [mask_obj(v, extra) for v in obj]
    if isinstance(obj, str):
        return mask(obj, extra)
    return obj


def mask_env_file(text: str) -> str:
    """Plik .env: każda wartość zmiennej o „sekretnej” nazwie → «ukryte». Reszta przez mask()."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if m and re.fullmatch(_SECRET_KEY_NAMES, m.group(2), re.I) and m.group(4).strip():
            line = f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}"
        out.append(line)
    return mask("\n".join(out))
