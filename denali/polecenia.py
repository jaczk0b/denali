"""Gotowe polecenia z karty Terminale.

Stała lista tylko do odczytu: panel wysyła agentowi sam klucz, a agent ma własną listę z treścią poleceń (COMMANDS
w denali-agent.py) — przejęty panel nie zleci niczego spoza niej. Tutaj są opisy dla ludzi; test pilnuje, żeby obie
listy się zgadzały. W terminalu SSH ta sama lista i własne polecenia administratora to tylko tekst do wklejenia.
"""
from dataclasses import dataclass

from .db import kv_get, kv_set

OUTPUT_MAX = 20000      # RESULT_MAX w agencie
CUSTOM_KEY = "terminal.presets"
CUSTOM_MAX = 50


@dataclass(frozen=True)
class Command:
    label: str
    text: str           # to samo, co uruchomi agent


COMMANDS = {
    "dyski": Command("Miejsce na dyskach", "df -h -x tmpfs -x devtmpfs"),
    "urzadzenia": Command("Dyski i partycje", "lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL"),
    "pamiec": Command("Pamięć", "free -h"),
    "czas_pracy": Command("Czas pracy i obciążenie", "uptime"),
    "procesy": Command("Procesy według CPU", "ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu"),
    "uslugi_bledy": Command("Usługi z błędem", "systemctl --failed --no-pager"),
    "logi_bledy": Command("Ostatnie błędy w logach", "journalctl -p err -n 50 --no-pager"),
    "docker": Command("Kontenery Docker", "docker ps -a"),
    "porty": Command("Otwarte porty", "ss -tulpn"),
    "adresy": Command("Adresy sieciowe", "ip -br address"),
}


def custom(conn):
    return kv_get(conn, CUSTOM_KEY, [])


def parse_custom(text):
    """„Nazwa | polecenie” w każdej linii → lista. Bez znaku | nazwą jest samo polecenie."""
    out = []
    for line in str(text or "").splitlines():
        label, sep, command = line.partition("|")
        label, command = label.strip(), command.strip()
        if not sep:
            label, command = line.strip(), line.strip()
        if command:
            out.append({"label": label[:60] or command[:60], "text": command[:500]})
    return out[:CUSTOM_MAX]


def save_custom(conn, text):
    kv_set(conn, CUSTOM_KEY, parse_custom(text))


def custom_text(conn):
    return "\n".join(f"{p['label']} | {p['text']}" for p in custom(conn))


def presets(conn):
    """Do panelu w terminalu: najpierw własne."""
    return custom(conn) + [{"label": c.label, "text": c.text} for c in COMMANDS.values()]
