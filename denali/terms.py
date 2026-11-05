"""Terminy przy urządzeniu: gwarancja, domena, licencja. Przypomnienie 30 dni przed, pilne 7 dni przed i po terminie."""
import time
from datetime import date, datetime

from sqlalchemy import delete, select

from .models import device, device_term

SOON_DAYS = 30
URGENT_DAYS = 7


class TermError(ValueError):
    pass


def parse(value):
    try:
        return date.fromisoformat(str(value or "").strip()).isoformat()
    except ValueError:
        raise TermError("Podaj datę w formacie RRRR-MM-DD.") from None


def add(conn, device_id, due_on, label):
    label = (label or "").strip()
    if not label:
        raise TermError("Napisz, czego dotyczy termin, np. „gwarancja”.")
    return conn.execute(device_term.insert().values(device_id=device_id, due_on=parse(due_on), label=label[:200])
                        ).inserted_primary_key[0]


def remove(conn, device_id, term_id):
    conn.execute(delete(device_term).where(device_term.c.id == term_id, device_term.c.device_id == device_id))


def for_device(conn, device_id):
    return conn.execute(select(device_term).where(device_term.c.device_id == device_id)
                        .order_by(device_term.c.due_on)).mappings().all()


def _describe(name, label, days):
    if days < 0:
        return f"{name}: {label} — minął {-days} dni temu"
    if days == 0:
        return f"{name}: {label} — dziś"
    return f"{name}: {label} — za {days} dni"


def due(conn, now=None):
    """Terminy w ciągu SOON_DAYS i zaległe; lista dla alertów i „Nadchodzących”."""
    now = now or time.time()
    out = []
    for t in conn.execute(select(device_term, device.c.name).join(device, device.c.id == device_term.c.device_id)
                          ).mappings():
        ts = datetime.fromisoformat(t["due_on"]).timestamp()
        days = round((ts - now) / 86400)
        if days > SOON_DAYS:
            continue
        out.append({"id": t["id"], "device_id": t["device_id"], "when": ts, "days": days,
                    "severity": "krytyczny" if days <= URGENT_DAYS else "ostrzezenie",
                    "text": _describe(t["name"], t["label"], days)})
    return sorted(out, key=lambda x: x["when"])
