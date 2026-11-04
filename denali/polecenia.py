"""Gotowe polecenia z karty Terminale: tylko odczyt, jedna stała lista.

Panel wysyła agentowi sam klucz, a agent ma własną listę z treścią poleceń (COMMANDS w denali-agent.py) — przejęty
panel nie zleci niczego spoza niej. Tutaj są opisy dla ludzi; test pilnuje, żeby obie listy się zgadzały.
"""
from dataclasses import dataclass

OUTPUT_MAX = 20000      # RESULT_MAX w agencie


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
