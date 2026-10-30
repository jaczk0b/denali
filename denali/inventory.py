"""Inwentarz: urządzenia, adresy, zależności i kable. Wszystkie zmiany idą przez te funkcje (walidacja + brak pętli)."""
import ipaddress
import re
import time

from sqlalchemy import or_, select

from .models import cable, dependency, device, device_address
from .rca import Edge, Graph

KINDS = {
    "router": "Router / brama", "switch": "Switch", "ap": "Access Point", "firewall": "Firewall", "modem": "Modem / ONT",
    "host": "Serwer fizyczny", "vm": "Maszyna wirtualna", "lxc": "Kontener LXC", "kontener": "Stack Dockera",
    "usluga": "Usługa / aplikacja", "vps": "VPS", "nas": "NAS", "ups": "UPS", "komputer": "Komputer",
    "drukarka": "Drukarka", "kamera": "Kamera", "telefon": "Telefon", "inne": "Inne",
}
# Do środka (na czym działa) wchodzi tylko to, co naprawdę w czymś siedzi — reguła z HLC.
NEST_IN = {
    "vm": {"host", "nas"},
    "lxc": {"host"},
    "kontener": {"host", "vm", "lxc", "nas", "vps"},
    "usluga": {"host", "vm", "lxc", "kontener", "nas", "vps"},
}
DEP_TYPES = {"zasilanie": "Zasilanie", "siec": "Sieć", "dane": "Dane", "usluga": "Usługa"}
ADDRESS_KINDS = ("ip", "mac", "hostname")
PULPIT_GROUPS = ("codzien", "zarzadzanie", "brak")
EDITABLE = ("name", "kind", "role", "criticality", "parent_id", "profile_id", "place", "serial", "notes_md", "icon",
            "app_url", "pulpit_group")

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class InventoryError(ValueError):
    pass


def get_device(conn, device_id):
    return conn.execute(select(device).where(device.c.id == device_id)).mappings().first()


def _require(conn, device_id):
    d = get_device(conn, device_id)
    if not d:
        raise InventoryError("Nie ma takiego urządzenia.")
    return d


def _clean(conn, fields, device_id=None, kind=None):
    out = {}
    for k, v in fields.items():
        if k not in EDITABLE:
            raise InventoryError(f"Nieznane pole: {k}")
        if k in ("name", "role", "place", "serial", "notes_md", "icon", "app_url"):
            v = (v or "").strip()
        out[k] = v
    if "name" in out and not out["name"]:
        raise InventoryError("Podaj nazwę.")
    if "name" in out and len(out["name"]) > 200:
        raise InventoryError("Nazwa jest za długa.")
    if "kind" in out and out["kind"] not in KINDS:
        raise InventoryError("Nieznany typ urządzenia.")
    if "criticality" in out and out["criticality"] not in (1, 2, 3):
        raise InventoryError("Krytyczność to P1, P2 albo P3.")
    if "pulpit_group" in out and out["pulpit_group"] not in PULPIT_GROUPS:
        raise InventoryError("Nieznana grupa pulpitu.")
    if out.get("app_url") and not re.match(r"^https?://", out["app_url"]):
        raise InventoryError("Adres aplikacji musi zaczynać się od http:// albo https://")
    if out.get("parent_id") is not None:
        err = nest_error(conn, device_id, out["parent_id"], kind or out.get("kind"))
        if err:
            raise InventoryError(err)
    return out


def nest_error(conn, child_id, parent_id, child_kind):
    """Czy child (typu child_kind) może działać na parent_id. None = może."""
    parent = get_device(conn, parent_id)
    if not parent:
        return "Nie ma takiego urządzenia nadrzędnego."
    if child_kind not in NEST_IN:
        return f"{KINDS.get(child_kind, child_kind)} nie działa na innym urządzeniu — zmień typ albo dodaj zależność."
    if parent["kind"] not in NEST_IN[child_kind]:
        return f"{KINDS[child_kind]} nie może działać na „{parent['name']}” ({KINDS.get(parent['kind'], parent['kind'])})."
    if child_id is not None and load_graph(conn).creates_cycle(child_id, parent_id):
        return "To zrobiłoby pętlę: urządzenie zależałoby samo od siebie."
    return None


def create_device(conn, name, kind, now=None, **fields):
    now = now or time.time()
    values = _clean(conn, {"name": name, "kind": kind, **fields}, kind=kind)
    return conn.execute(device.insert().values(**values, created_at=now, updated_at=now)).inserted_primary_key[0]


def update_device(conn, device_id, now=None, **fields):
    cur = _require(conn, device_id)
    kind = fields.get("kind", cur["kind"])
    if "kind" in fields and "parent_id" not in fields and cur["parent_id"] is not None:
        fields["parent_id"] = cur["parent_id"]          # zmiana typu musi dalej pasować do rodzica
    values = _clean(conn, fields, device_id=device_id, kind=kind)
    conn.execute(device.update().where(device.c.id == device_id).values(**values, updated_at=now or time.time()))


def delete_device(conn, device_id):
    conn.execute(device.delete().where(device.c.id == device_id))


def normalize_address(kind, value):
    value = (value or "").strip().lower()
    if kind == "ip":
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise InventoryError(f"„{value}” to nie jest adres IP.") from None
    if kind == "mac":
        value = value.replace("-", ":")
        if not _MAC.match(value):
            raise InventoryError(f"„{value}” to nie jest adres MAC.")
        return value
    if kind == "hostname":
        if not _HOSTNAME.match(value):
            raise InventoryError(f"„{value}” to nie jest poprawna nazwa hosta.")
        return value
    raise InventoryError("Adres to IP, MAC albo nazwa hosta.")


def add_address(conn, device_id, kind, value, source="manual", primary=False):
    _require(conn, device_id)
    value = normalize_address(kind, value)
    exists = conn.execute(select(device_address.c.id).where(
        device_address.c.device_id == device_id, device_address.c.kind == kind,
        device_address.c.value == value)).first()
    if exists:
        return exists.id
    if primary:
        conn.execute(device_address.update().where(device_address.c.device_id == device_id,
                                                   device_address.c.kind == kind).values(is_primary=False))
    return conn.execute(device_address.insert().values(device_id=device_id, kind=kind, value=value, source=source,
                                                       is_primary=primary)).inserted_primary_key[0]


def find_by_address(conn, kind, value):
    """Urządzenie o danym adresie (do łączenia obiektów z różnych integracji). None, gdy brak albo niejednoznaczne."""
    try:
        value = normalize_address(kind, value)
    except InventoryError:
        return None
    rows = conn.execute(select(device_address.c.device_id).distinct().where(
        device_address.c.kind == kind, device_address.c.value == value)).all()
    return rows[0].device_id if len(rows) == 1 else None


def add_dependency(conn, device_id, depends_on_id, type="siec", hard=True, note="", source="manual"):
    _require(conn, device_id)
    _require(conn, depends_on_id)
    if type not in DEP_TYPES:
        raise InventoryError("Nieznany rodzaj zależności.")
    exists = conn.execute(select(dependency.c.id).where(
        dependency.c.device_id == device_id, dependency.c.depends_on_id == depends_on_id,
        dependency.c.type == type)).first()
    if exists:
        return exists.id
    if load_graph(conn).creates_cycle(device_id, depends_on_id):
        raise InventoryError("To zrobiłoby pętlę: urządzenie zależałoby samo od siebie.")
    return conn.execute(dependency.insert().values(device_id=device_id, depends_on_id=depends_on_id, type=type,
                                                   hard=hard, note=(note or "").strip(), source=source)
                        ).inserted_primary_key[0]


def remove_dependency(conn, dep_id):
    conn.execute(dependency.delete().where(dependency.c.id == dep_id))


def add_cable(conn, a_device_id, b_device_id, a_port="", b_port="", label=""):
    """Strona A — bliżej routera (switch, router), strona B — wpięte urządzenie."""
    _require(conn, a_device_id)
    _require(conn, b_device_id)
    if load_graph(conn).creates_cycle(b_device_id, a_device_id):
        raise InventoryError("Ten kabel zrobiłby pętlę — sprawdź, która strona jest bliżej routera.")
    return conn.execute(cable.insert().values(a_device_id=a_device_id, b_device_id=b_device_id,
                                              a_port=(a_port or "").strip()[:40], b_port=(b_port or "").strip()[:40],
                                              label=(label or "").strip()[:200])).inserted_primary_key[0]


def remove_cable(conn, cable_id):
    conn.execute(cable.delete().where(cable.c.id == cable_id))


def device_cables(conn, device_id):
    return conn.execute(select(cable).where(or_(cable.c.a_device_id == device_id, cable.c.b_device_id == device_id))
                        ).mappings().all()


def load_graph(conn):
    devs = conn.execute(select(device.c.id, device.c.name, device.c.parent_id)).all()
    edges = [Edge(d.id, d.parent_id, True, "hosting") for d in devs if d.parent_id]
    edges += [Edge(r.device_id, r.depends_on_id, bool(r.hard), r.type) for r in conn.execute(select(dependency))]
    edges += [Edge(r.b_device_id, r.a_device_id, True, "kabel") for r in conn.execute(select(cable))]
    return Graph(edges, {d.id: d.name for d in devs})
