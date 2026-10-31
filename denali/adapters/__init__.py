"""Adaptery integracji. Każdy system (Proxmox, Komodo, TrueNAS…) to jedna klasa z tym samym interfejsem.

Adapter tylko czyta: zgłasza obiekty (trafiają do kolejki „do zatwierdzenia”), ich bieżący stan
i dostępne aktualizacje.
Nowy typ urządzenia = nowa klasa tutaj, bez zmian w rdzeniu.
"""
from dataclasses import dataclass, field
from typing import Any

STATES = ("ok", "warn", "down", "off", "unknown")


class AdapterError(Exception):
    """Błąd z czytelnym opisem dla człowieka (pokazywany w panelu)."""


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"          # text | url | secret | bool | textarea
    required: bool = True
    help: str = ""
    default: Any = ""


@dataclass
class Found:
    """Obiekt do kolejki „do zatwierdzenia” (format `proposed` opisany w discovery.py)."""
    external_id: str
    proposed: dict


@dataclass
class State:
    external_id: str
    state: str                  # ok | warn | down | off (wyłączone celowo) | unknown
    summary: str = ""
    data: dict = field(default_factory=dict)
    # Stan, który nie jest osobnym urządzeniem, tylko informacją o istniejącym (kopia VM w PBS, pula w TrueNAS).
    # {"ref": external_id tej samej integracji} albo {"integration_type": "proxmox", "external_ids": ["qemu/100"]}.
    # Podpina się sam pod urządzenie, które już jest — bez kolejki „do zatwierdzenia”.
    attach: dict | None = None
    label: str = ""             # nazwa obiektu do listy „Co widać” (np. „kopie VM 100”)


@dataclass
class Update:
    """Dostępna aktualizacja obiektu (pakiet na węźle, nowszy obraz usługi stacka)."""
    external_id: str
    component: str              # nazwa pakietu albo usługi
    kind: str = "pakiet"        # pakiet | obraz | system
    current: str = ""
    available: str = ""
    security: bool = False


def apt_updates(external_id, packages):
    """Lista z `/nodes/<węzeł>/apt/update` (Proxmox VE i PBS — to samo, co ich panel pokazuje w „Updates”)."""
    out = []
    for p in packages or []:
        if not isinstance(p, dict) or not p.get("Package"):
            continue
        origin = f"{p.get('Origin', '')} {p.get('Section', '')}".lower()
        out.append(Update(external_id, str(p["Package"]), "pakiet", str(p.get("OldVersion") or ""),
                          str(p.get("Version") or ""), "security" in origin))
    return out


@dataclass
class PollResult:
    found: list = field(default_factory=list)
    states: list = field(default_factory=list)
    # None = źródło nie podaje aktualizacji (lista zostaje bez zmian); [] = sprawdzone, nic nie ma
    updates: list | None = None


class Adapter:
    type = ""
    label = ""
    fields: list = []
    default_poll = 60
    push = False                # True: źródło samo wysyła dane (agent) — worker go nie odpytuje

    def __init__(self, config: dict, secrets: dict, transport=None):
        self.config = config or {}
        self.secrets = secrets or {}
        self.transport = transport          # w testach: httpx.MockTransport z nagranymi odpowiedziami

    @classmethod
    def validate(cls, config: dict, secrets: dict) -> None:
        """Rzuca AdapterError, gdy konfiguracja jest niepełna albo zła."""
        for f in cls.fields:
            value = (secrets if f.kind == "secret" else config).get(f.key)
            if f.required and f.kind != "bool" and not str(value or "").strip():
                raise AdapterError(f"Uzupełnij pole „{f.label}”.")

    async def test(self) -> str:
        """Sprawdza połączenie i uprawnienia. Zwraca krótki opis, co widać."""
        raise NotImplementedError

    async def poll(self) -> PollResult:
        raise NotImplementedError


def registry() -> dict:
    from ..agents import Agent
    from .komodo import Komodo
    from .pbs import Pbs
    from .proxmox import Proxmox
    from .tplink import Tplink
    from .truenas import Truenas
    return {a.type: a for a in (Proxmox, Komodo, Pbs, Truenas, Tplink, Agent)}
