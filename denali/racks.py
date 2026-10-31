"""Szafa (rack): co na której wysokości stoi. Sama dokumentacja — nic tu nie przychodzi z integracji.

Pozycja to urządzenie (switch, serwer, UPS) albo sam opis (patch panel, półka, zaślepka). U liczymy od dołu,
jak na szynach szafy: pozycja „u=5, height=2” zajmuje U5 i U6. Z przodu i z tyłu można zamontować różne rzeczy
na tej samej wysokości (np. listwa zasilająca z tyłu za półką).
"""
from sqlalchemy import func, select

from .models import device, rack, rack_item

MAX_UNITS = 60


class RackError(ValueError):
    pass


def _int(v, what):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        raise RackError(f"{what}: podaj liczbę.") from None


def save_rack(conn, name, units, place="", notes="", rack_id=None):
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise RackError("Podaj nazwę szafy (do 100 znaków).")
    units = _int(units, "Wysokość szafy")
    if not 1 <= units <= MAX_UNITS:
        raise RackError(f"Wysokość szafy: od 1 do {MAX_UNITS} U.")
    dup = conn.execute(select(rack.c.id).where(rack.c.name == name)).scalar()
    if dup and dup != rack_id:
        raise RackError("Jest już szafa o tej nazwie.")
    values = {"name": name, "units": units, "place": (place or "").strip()[:200], "notes": (notes or "").strip()[:500]}
    if rack_id is None:
        return conn.execute(rack.insert().values(**values)).inserted_primary_key[0]
    top = conn.execute(select(func.max(rack_item.c.u + rack_item.c.height - 1))
                       .where(rack_item.c.rack_id == rack_id)).scalar() or 0
    if units < top:
        raise RackError("Coś stoi wyżej niż nowa wysokość szafy — najpierw to przesuń.")
    conn.execute(rack.update().where(rack.c.id == rack_id).values(**values))
    return rack_id


def delete_rack(conn, rack_id):
    conn.execute(rack.delete().where(rack.c.id == rack_id))


def place(conn, rack_id, u, height=1, device_id=None, label="", back=False, item_id=None):
    """Umieść urządzenie (albo opis) w szafie. Urządzenie stoi tylko w jednym miejscu — przeniesienie zastępuje."""
    r = conn.execute(select(rack).where(rack.c.id == rack_id)).mappings().first()
    if not r:
        raise RackError("Nie ma takiej szafy.")
    u, height = _int(u, "Pozycja (U)"), _int(height or 1, "Wysokość (U)")
    label = (label or "").strip()[:200]
    if device_id is None and not label:
        raise RackError("Wybierz urządzenie albo wpisz, co to jest (np. patch panel 24 porty).")
    if device_id is not None and not conn.execute(select(device.c.id).where(device.c.id == device_id)).scalar():
        raise RackError("Nie ma takiego urządzenia.")
    if not 1 <= height <= r["units"]:
        raise RackError(f"Wysokość: od 1 do {r['units']} U.")
    if u < 1 or u + height - 1 > r["units"]:
        raise RackError(f"Szafa „{r['name']}” ma {r['units']} U — pozycja U{u}–U{u + height - 1} się nie mieści.")
    q = select(rack_item, device.c.name).outerjoin(device, device.c.id == rack_item.c.device_id).where(
        rack_item.c.rack_id == rack_id, rack_item.c.back == bool(back))
    for o in conn.execute(q).mappings():
        if o["id"] == item_id or (device_id is not None and o["device_id"] == device_id):
            continue
        if u <= o["u"] + o["height"] - 1 and o["u"] <= u + height - 1:
            raise RackError(f"U{o['u']}{'–U' + str(o['u'] + o['height'] - 1) if o['height'] > 1 else ''} "
                            f"zajmuje już „{o['name'] or o['label']}”.")
    values = {"rack_id": rack_id, "u": u, "height": height, "device_id": device_id, "label": label, "back": bool(back)}
    if device_id is not None:
        conn.execute(rack_item.delete().where(rack_item.c.device_id == device_id, rack_item.c.id != (item_id or 0)))
    if item_id:
        conn.execute(rack_item.update().where(rack_item.c.id == item_id).values(**values))
        return item_id
    return conn.execute(rack_item.insert().values(**values)).inserted_primary_key[0]


def remove_item(conn, item_id):
    conn.execute(rack_item.delete().where(rack_item.c.id == item_id))


def for_device(conn, device_id):
    return conn.execute(select(rack_item, rack.c.name.label("rack_name"))
                        .join(rack, rack.c.id == rack_item.c.rack_id)
                        .where(rack_item.c.device_id == device_id)).mappings().first()


def overview(conn):
    """Szafy z pozycjami ułożonymi od góry (jak się patrzy na szafę): [{"rack", "front", "back", "entries"}]."""
    out = []
    for r in conn.execute(select(rack).order_by(rack.c.name)).mappings():
        items = conn.execute(select(rack_item, device.c.name, device.c.kind)
                             .outerjoin(device, device.c.id == rack_item.c.device_id)
                             .where(rack_item.c.rack_id == r["id"]).order_by(rack_item.c.u.desc())).mappings().all()
        out.append({"rack": r, "entries": items, "front": _rows(r["units"], [i for i in items if not i["back"]]),
                    "back": _rows(r["units"], [i for i in items if i["back"]]),
                    "used": sum(i["height"] for i in items if not i["back"])})
    return out


def _rows(units, items):
    """Siatka od góry: pozycja zajmuje `height` wierszy, puste U to osobne wiersze (do CSS grid-row)."""
    taken = {}
    for i in items:
        taken[i["u"] + i["height"] - 1] = i
    rows, u = [], units
    while u >= 1:
        i = taken.get(u)
        if i:
            rows.append({"item": i, "top": u, "row": units - u + 1, "span": i["height"]})
            u -= i["height"]
        else:
            rows.append({"item": None, "top": u, "row": units - u + 1, "span": 1})
            u -= 1
    return rows
