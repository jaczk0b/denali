"""Adaptery integracji. Każdy system (Proxmox, Komodo, TrueNAS…) to jedna klasa z tym samym interfejsem.

Adapter tylko czyta: zgłasza obiekty (trafiają do kolejki „do zatwierdzenia”) i ich bieżący stan.
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


@dataclass
class PollResult:
    found: list = field(default_factory=list)
    states: list = field(default_factory=list)


class Adapter:
    type = ""
    label = ""
    fields: list = []
    default_poll = 60

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
    from .komodo import Komodo
    from .proxmox import Proxmox
    return {a.type: a for a in (Proxmox, Komodo)}
