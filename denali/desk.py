"""Ikony na pulpicie: kategorie, kafle i propozycje, co dodać.

Kafel to dowolny skrót (aplikacja w sieci albo strona panelu). Powiązany z urządzeniem bierze z niego brakującą
nazwę, adres i skrót na ikonie, a na pulpicie pokazuje jego stan.
"""
import re

from sqlalchemy import func, select

from .models import desk_category, desk_tile, device, integration

DEFAULT_CATEGORY = "Na co dzień"
MAX_ICON = 3
# Strony panelu: szybkie przejście (Ctrl+K) i propozycje ikon.
PAGES = [("Pulpit", "/"), ("Mapa", "/infrastruktura/mapa"), ("Lista urządzeń", "/urzadzenia"),
         ("Szafa", "/infrastruktura/szafa"), ("Adresy IP", "/infrastruktura/adresy"),
         ("Incydenty", "/incydenty"), ("Aktualizacje", "/aktualizacje"), ("Procedury", "/procedury"),
         ("Co jeśli…? (symulacja awarii)", "/infrastruktura/mapa?co_jesli=0"), ("Konto i sesje", "/konto")]
ADMIN_PAGES = [("Do zatwierdzenia", "/zatwierdz"), ("Integracje", "/admin/integracje"),
               ("Powiadomienia", "/admin/powiadomienia"), ("Akcje", "/akcje"), ("Terminale", "/terminale"),
               ("Audyt", "/admin/audyt"), ("Nowe urządzenie", "/urzadzenia/nowe")]
_ADMIN_PATHS = {url for _, url in ADMIN_PAGES}
_NOT_A_TILE = {"/", "/konto"}                     # pulpit i konto są zawsze pod ręką


class DeskError(ValueError):
    pass


def _external(url):
    return url.lower().startswith(("http://", "https://"))


def _same_url(url):
    return url.strip().rstrip("/").lower()


def check_url(url):
    """Pełny adres http(s) albo ścieżka panelu. „//host” i „/\\host” przeglądarka potraktowałaby jak inny serwer."""
    url = (url or "").strip()
    if len(url) > 500:
        raise DeskError("Adres jest za długi.")
    if url and not re.match(r"^(https?://[^\s/]\S*|/(?![/\\])\S*)$", url, re.IGNORECASE):
        raise DeskError("Adres musi zaczynać się od http://, https:// albo / (strona panelu).")
    return url


# ───────────────────────── odczyt ─────────────────────────
def categories(conn):
    return conn.execute(select(desk_category).order_by(desk_category.c.position, desk_category.c.id)).mappings().all()


def _view(r):
    url = r["url"] or r["device_url"] or (f"/urzadzenia/{r['device_id']}" if r["device_id"] else "")
    return {"id": r["id"], "category_id": r["category_id"], "device_id": r["device_id"],
            "name": r["name"] or r["device_name"] or "", "url": url, "icon": r["icon"] or r["device_icon"] or "",
            "external": _external(url), "own_name": r["name"], "own_url": r["url"], "own_icon": r["icon"]}


def tiles(conn, role="admin"):
    """Kafle z wartościami dziedziczonymi z urządzenia. Podgląd nie widzi skrótów do stron tylko dla administratora."""
    rows = conn.execute(select(desk_tile, device.c.name.label("device_name"), device.c.app_url.label("device_url"),
                               device.c.icon.label("device_icon"))
                        .select_from(desk_tile.outerjoin(device, device.c.id == desk_tile.c.device_id))
                        .order_by(desk_tile.c.position, desk_tile.c.id)).mappings().all()
    return [v for v in map(_view, rows) if role == "admin" or v["url"] not in _ADMIN_PATHS]


def board(conn, role="admin"):
    all_tiles = tiles(conn, role)
    return [{**c, "tiles": [t for t in all_tiles if t["category_id"] == c["id"]]} for c in categories(conn)]


def get_tile(conn, tile_id):
    return next((t for t in tiles(conn) if t["id"] == tile_id), None)


def device_category(conn, device_id):
    return conn.execute(select(desk_category).join(desk_tile, desk_tile.c.category_id == desk_category.c.id)
                        .where(desk_tile.c.device_id == device_id)).mappings().first()


def find(conn, q, role="admin"):
    low = q.lower()
    return [t for t in tiles(conn, role) if low in t["name"].lower() or low in t["url"].lower()]


def suggestions(conn):
    """Czego jeszcze nie ma na pulpicie, a panel już zna: {key, name, url, group}. Klucz rozwiązuje add_suggestion."""
    current = tiles(conn)
    pinned = {t["device_id"] for t in current}
    seen = {_same_url(t["url"]) for t in current}
    out = []

    def offer(key, name, url, group):
        if _same_url(url) not in seen:
            seen.add(_same_url(url))
            out.append({"key": key, "name": name, "url": url, "group": group})

    for d in conn.execute(select(device.c.id, device.c.name, device.c.app_url).where(device.c.app_url != "")
                          .order_by(device.c.name)).mappings():
        if d["id"] not in pinned:
            offer(f"urzadzenie:{d['id']}", d["name"], d["app_url"], "Aplikacje")
    for i in conn.execute(select(integration.c.id, integration.c.name, integration.c.config)
                          .order_by(integration.c.name)).mappings():
        url = str((i["config"] or {}).get("url") or "")
        if _external(url):
            offer(f"integracja:{i['id']}", i["name"], url, "Integracje")
    for name, url in PAGES + ADMIN_PAGES:
        if url not in _NOT_A_TILE:
            offer(f"strona:{url}", name.removesuffix(" (symulacja awarii)"), url, "Strony panelu")
    return out


# ───────────────────────── zmiany: kategorie ─────────────────────────
def _next_position(conn, table, *where):
    return (conn.execute(select(func.max(table.c.position)).where(*where)).scalar() or 0) + 1


def _category_name(name):
    name = (name or "").strip()
    if not name:
        raise DeskError("Podaj nazwę kategorii.")
    if len(name) > 100:
        raise DeskError("Nazwa kategorii jest za długa.")
    return name


def _named(conn, name):
    return next((c for c in categories(conn) if c["name"].lower() == name.lower()), None)


def ensure_category(conn, name):
    """Id kategorii o tej nazwie — istniejącej albo nowej."""
    name = _category_name(name)
    found = _named(conn, name)
    if found:
        return found["id"]
    return conn.execute(desk_category.insert().values(name=name, position=_next_position(conn, desk_category))
                        ).inserted_primary_key[0]


def rename_category(conn, category_id, name):
    name = _category_name(name)
    found = _named(conn, name)
    if found and found["id"] != category_id:
        raise DeskError("Taka kategoria już jest.")
    conn.execute(desk_category.update().where(desk_category.c.id == category_id).values(name=name))


def delete_category(conn, category_id):
    conn.execute(desk_category.delete().where(desk_category.c.id == category_id))


def _swap(conn, table, ids, row_id, step):
    """Przesuwa wiersz o jedno miejsce i przepisuje pozycje całej listy (po migracji mogą się powtarzać)."""
    if row_id not in ids:
        return
    i = ids.index(row_id)
    j = i + step
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for pos, one in enumerate(ids):
        conn.execute(table.update().where(table.c.id == one).values(position=pos))


def move_category(conn, category_id, step):
    _swap(conn, desk_category, [c["id"] for c in categories(conn)], category_id, step)


# ───────────────────────── zmiany: kafle ─────────────────────────
def _clean(conn, category_id, name, url, icon, device_id, tile_id=None):
    name, icon, url = (name or "").strip(), (icon or "").strip(), check_url(url)
    if not conn.execute(select(desk_category.c.id).where(desk_category.c.id == category_id)).first():
        raise DeskError("Wybierz kategorię.")
    if len(name) > 200:
        raise DeskError("Nazwa jest za długa.")
    if len(icon) > MAX_ICON:
        raise DeskError(f"Skrót na ikonie: najwyżej {MAX_ICON} znaki.")
    if device_id is None:
        if not name or not url:
            raise DeskError("Podaj nazwę i adres albo wybierz urządzenie.")
    else:
        if not conn.execute(select(device.c.id).where(device.c.id == device_id)).first():
            raise DeskError("Nie ma takiego urządzenia.")
        other = conn.execute(select(desk_tile.c.id).where(desk_tile.c.device_id == device_id)).scalar()
        if other is not None and other != tile_id:
            raise DeskError("To urządzenie ma już ikonę na pulpicie.")
    return {"category_id": category_id, "name": name, "url": url, "icon": icon, "device_id": device_id}


def add_tile(conn, category_id, name="", url="", icon="", device_id=None):
    values = _clean(conn, category_id, name, url, icon, device_id)
    position = _next_position(conn, desk_tile, desk_tile.c.category_id == category_id)
    return conn.execute(desk_tile.insert().values(**values, position=position)).inserted_primary_key[0]


def update_tile(conn, tile_id, category_id, name="", url="", icon="", device_id=None):
    cur = get_tile(conn, tile_id)
    if not cur:
        raise DeskError("Nie ma takiej ikony.")
    values = _clean(conn, category_id, name, url, icon, device_id, tile_id=tile_id)
    if cur["category_id"] != category_id:
        values["position"] = _next_position(conn, desk_tile, desk_tile.c.category_id == category_id)
    conn.execute(desk_tile.update().where(desk_tile.c.id == tile_id).values(**values))


def delete_tile(conn, tile_id):
    conn.execute(desk_tile.delete().where(desk_tile.c.id == tile_id))


def move_tile(conn, tile_id, step):
    cur = get_tile(conn, tile_id)
    if cur:
        _swap(conn, desk_tile, [t["id"] for t in tiles(conn) if t["category_id"] == cur["category_id"]], tile_id, step)


def pin_device(conn, device_id, category_id):
    """Ikona urządzenia w tej kategorii; None zdejmuje ją z pulpitu."""
    cur = conn.execute(select(desk_tile).where(desk_tile.c.device_id == device_id)).mappings().first()
    if category_id is None:
        conn.execute(desk_tile.delete().where(desk_tile.c.device_id == device_id))
    elif not cur:
        add_tile(conn, category_id, device_id=device_id)
    elif cur["category_id"] != category_id:
        update_tile(conn, cur["id"], category_id, cur["name"], cur["url"], cur["icon"], device_id)


def add_suggestion(conn, key, category_id):
    """Dodaje propozycję po kluczu — adres bierze z bazy, a nie z formularza."""
    found = next((s for s in suggestions(conn) if s["key"] == key), None)
    if not found:
        raise DeskError("Tej propozycji już nie ma — pewnie jest na pulpicie.")
    if key.startswith("urzadzenie:"):
        return add_tile(conn, category_id, device_id=int(key.split(":", 1)[1]))
    return add_tile(conn, category_id, found["name"], found["url"])
