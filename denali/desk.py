"""Ikony na pulpicie: kategorie, kafle i propozycje, co dodać.

Kafel to dowolny skrót: aplikacja w sieci, strona panelu (też terminal konkretnej maszyny) albo adres do skopiowania
(udział SMB i inne adresy, których przeglądarka nie otworzy). Rodzaj wynika z samego adresu. Powiązany z urządzeniem
bierze z niego brakującą nazwę, adres i skrót na ikonie, a na pulpicie pokazuje jego stan.
"""
import re
import secrets

from sqlalchemy import func, select

from .db import kv_get, kv_set
from .models import desk_category, desk_tile, desk_widget, device, device_address, integration, share

DEFAULT_CATEGORY = "Na co dzień"
MAX_ICON = 3
# Strony panelu: szybkie przejście (Ctrl+K) i propozycje ikon.
PAGES = [("Pulpit", "/"), ("Mapa", "/infrastruktura/mapa"), ("Lista urządzeń", "/urzadzenia"),
         ("Szafa", "/infrastruktura/szafa"), ("Adresy IP", "/infrastruktura/adresy"),
         ("Incydenty", "/incydenty"), ("Alerty", "/alerty"), ("Aktualizacje", "/aktualizacje"), ("Oś czasu", "/os-czasu"),
         ("Procedury", "/procedury"),
         ("Co jeśli…? (symulacja awarii)", "/infrastruktura/mapa?co_jesli=0"), ("Konto i sesje", "/konto")]
ADMIN_PAGES = [("Do zatwierdzenia", "/zatwierdz"), ("Integracje", "/admin/integracje"),
               ("Kanały powiadomień", "/admin/powiadomienia"), ("Akcje", "/akcje"), ("Terminale", "/terminale"),
               ("Audyt", "/admin/audyt"), ("Nowe urządzenie", "/urzadzenia/nowe")]
_ADMIN_PATHS = {url for _, url in ADMIN_PAGES}
_ADMIN_PREFIXES = ("/admin/", "/terminale/", "/akcje/", "/zatwierdz/")
# Adresy tylko do skopiowania: UNC (\\\\nas\\zdjecia) i schematy, których przeglądarka nie otworzy. Nigdy nie trafiają do href.
_COPY_RE = re.compile(r"^(\\\\[^\\/\s]+(\\[^\\/\x00-\x1f]+)*\\?|(smb|nfs|afp|ftp|sftp|ssh|rdp|vnc)://\S+)$", re.IGNORECASE)
_NOT_A_TILE = {"/", "/konto"}                     # pulpit i konto są zawsze pod ręką


# Widżety Statystyk: rodzaj → (nazwa, skąd dane, co pokazuje). Dane liczy deskview.WIDGET_DATA.
WIDGETS = {"proxmox": ("Proxmox", "Proxmox", "CPU, RAM, maszyny, czas działania"),
           "pula": ("Pula dyskowa", "TrueNAS", "zajętość, stan, dyski, temperatury"),
           "kopie": ("Kopie zapasowe", "TrueNAS, PBS", "snapshoty, replikacja, kopie maszyn"),
           "kontenery": ("Kontenery", "Komodo", "stacki, problemy, nowe obrazy"),
           "siec": ("Sieć", "OpenWrt, TP-Link", "urządzenia w sieci, Wi-Fi, nowe adresy"),
           "certyfikaty": ("Certyfikaty", "sprawdzenia HTTP", "ważne i najbliżej wygaśnięcia")}
# Panel boczny: klucz, nazwa, czy gotowe. Asystent czeka na swoją funkcję: miejsce w układzie już ma.
SIDE = [("uwaga", "Wymaga uwagi", True), ("awaria", "Skąd ta awaria", True), ("zdarzenia", "Ostatnie zdarzenia", True),
        ("nadchodzace", "Nadchodzące", True), ("asystent", "Asystent AI", False)]
LOOK = {"stats_at": None, "show_host": True, "mark_lan": True, "icons": "litera"}
ICON_STYLES = ("litera", "logo")
LAYOUT_KEY = "desk.uklad"
TRASH_KEY = "desk.kosz"
TRASH_SIZE = 20
NEW_CATEGORY = "Nowa kategoria"


class DeskError(ValueError):
    pass


def _external(url):
    return url.lower().startswith(("http://", "https://"))


def is_copy(url):
    return bool(_COPY_RE.match(url or ""))


def _admin_only(url):
    return url in _ADMIN_PATHS or url.startswith(_ADMIN_PREFIXES)


def _same_url(url):
    return url.strip().rstrip("/").lower()


def check_url(url):
    """Pełny adres http(s), ścieżka panelu albo adres do skopiowania. „//host” i „/\\host” przeglądarka potraktowałaby
    jak inny serwer."""
    url = (url or "").strip()
    if len(url) > 500:
        raise DeskError("Adres jest za długi.")
    if url and not is_copy(url) and not re.match(r"^(https?://[^\s/]\S*|/(?![/\\])\S*)$", url, re.IGNORECASE):
        raise DeskError("Adres musi zaczynać się od http://, https://, / (strona panelu), \\\\serwer\\udział albo smb://, nfs://, ssh://…")
    return url


# ───────────────────────── odczyt ─────────────────────────
def categories(conn):
    return conn.execute(select(desk_category).order_by(desk_category.c.position, desk_category.c.id)).mappings().all()


def _view(r):
    url = r["url"] or r["device_url"] or (f"/urzadzenia/{r['device_id']}" if r["device_id"] else "")
    return {"id": r["id"], "category_id": r["category_id"], "device_id": r["device_id"],
            "name": r["name"] or r["device_name"] or "", "url": url, "icon": r["icon"] or r["device_icon"] or "",
            "logo": r["logo"], "external": _external(url), "copy_only": is_copy(url), "own_name": r["name"], "own_url": r["url"], "own_icon": r["icon"]}


def tiles(conn, role="admin"):
    """Kafle z wartościami dziedziczonymi z urządzenia. Podgląd nie widzi skrótów do stron tylko dla administratora."""
    rows = conn.execute(select(desk_tile, device.c.name.label("device_name"), device.c.app_url.label("device_url"),
                               device.c.icon.label("device_icon"))
                        .select_from(desk_tile.outerjoin(device, device.c.id == desk_tile.c.device_id))
                        .order_by(desk_tile.c.position, desk_tile.c.id)).mappings().all()
    return [v for v in map(_view, rows) if role == "admin" or not _admin_only(v["url"])]


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
    # adres do skopiowania nie jest stroną, do której można przejść
    return [t for t in tiles(conn, role) if not t["copy_only"] and (low in t["name"].lower() or low in t["url"].lower())]


def terminals(conn):
    """[(„Terminal: nazwa”, adres)] maszyn, które mają adres na karcie."""
    with_address = select(device_address.c.device_id).where(device_address.c.kind.in_(("ip", "hostname")))
    rows = conn.execute(select(device.c.id, device.c.name).where(device.c.id.in_(with_address)).order_by(device.c.name))
    return [(f"Terminal: {r.name}", f"/terminale/{r.id}") for r in rows]


def url_choices(conn):
    """Podpowiedzi pola „Adres”: strony panelu i terminale."""
    return [{"name": n, "url": u} for n, u in PAGES + ADMIN_PAGES + terminals(conn) if u not in _NOT_A_TILE]


def suggestions(conn, with_widgets=False):
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
    for name, url in terminals(conn):
        offer(f"strona:{url}", name, url, "Terminale")
    for s in conn.execute(select(share.c.title, share.c.target).where(share.c.kind == "adres").order_by(share.c.position)):
        if is_copy(s.target):
            offer(f"adres:{s.target}", s.title, s.target, "Do skopiowania")
    if with_widgets:
        have = {w["kind"] for w in widgets(conn)}
        out += [{"key": f"widzet:{k}", "name": v[0], "url": v[2], "group": "Statystyki"} for k, v in WIDGETS.items() if k not in have]
    return out


def catalog(conn):
    """Lista okna „Dodaj do pulpitu”: co można dodać i co już jest. tab: aplikacje | statystyki | strony | terminale."""
    tabs = {"Aplikacje": "aplikacje", "Integracje": "aplikacje", "Do skopiowania": "aplikacje", "Statystyki": "statystyki",
            "Strony panelu": "strony", "Terminale": "terminale"}
    out = [{"tab": tabs[s["group"]], "name": s["name"], "detail": s["url"], "icon": s["name"][:1].upper(), "key": s["key"]}
           for s in suggestions(conn, with_widgets=True)]
    for t in tiles(conn):
        tab = "terminale" if t["url"].startswith("/terminale/") else "strony" if t["url"].startswith("/") else "aplikacje"
        out.append({"tab": tab, "name": t["name"], "detail": t["url"], "icon": t["name"][:1].upper(), "key": ""})
    for w in widgets(conn):
        name, _source, what = WIDGETS.get(w["kind"], (w["kind"], "", ""))
        out.append({"tab": "statystyki", "name": w["title"] or name, "detail": what, "icon": name[:1].upper(), "key": ""})
    out.sort(key=lambda x: (x["key"] == "", x["name"].lower()))
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
    found = next((s for s in suggestions(conn, with_widgets=True) if s["key"] == key), None)
    if not found:
        raise DeskError("Tej propozycji już nie ma — pewnie jest na pulpicie.")
    if key.startswith("widzet:"):
        return add_widget(conn, key.split(":", 1)[1])
    if key.startswith("urzadzenie:"):
        return add_tile(conn, category_id, device_id=int(key.split(":", 1)[1]))
    return add_tile(conn, category_id, found["name"], found["url"])


# ───────────────────────── widżety ─────────────────────────
def widgets(conn):
    return conn.execute(select(desk_widget).order_by(desk_widget.c.position, desk_widget.c.id)).mappings().all()


def add_widget(conn, kind):
    if kind not in WIDGETS:
        raise DeskError("Nie ma takiej statystyki.")
    return conn.execute(desk_widget.insert().values(kind=kind, position=_next_position(conn, desk_widget))).inserted_primary_key[0]


def resize_widget(conn, widget_id, size):
    if size not in (1, 2):
        raise DeskError("Rozmiar to 1 albo 2.")
    conn.execute(desk_widget.update().where(desk_widget.c.id == widget_id).values(size=size))


# ───────────────────────── układ i wygląd ─────────────────────────
def look(conn):
    return {**LOOK, **(kv_get(conn, LAYOUT_KEY) or {})}


def side(conn):
    """Sekcje panelu bocznego w zapisanej kolejności. Nowa sekcja dochodzi na koniec, a niegotowa jest wyłączona."""
    saved = {s["key"]: s for s in look(conn).get("side") or [] if isinstance(s, dict)}
    known = {k: (label, ready) for k, label, ready in SIDE}
    order = [k for k in saved if k in known] + [k for k, _l, _r in SIDE if k not in saved]
    return [{"key": k, "label": known[k][0], "ready": known[k][1], "on": known[k][1] and bool(saved.get(k, {}).get("on", True))} for k in order]


def sections(board_rows, settings):
    """Kolejność na pulpicie: kategorie i Statystyki jako jeden, przesuwalny blok."""
    out = [{"kind": "kategoria", **c} for c in board_rows]
    at = settings["stats_at"]
    at = len(out) if not isinstance(at, int) else max(0, min(at, len(out)))
    out.insert(at, {"kind": "statystyki"})
    return out


def save_look(conn, values):
    current = look(conn)
    for key in ("show_host", "mark_lan"):
        if key in values:
            current[key] = bool(values[key])
    if values.get("icons") in ICON_STYLES:
        current["icons"] = values["icons"]
    if isinstance(values.get("side"), list):
        known = {k for k, _l, _r in SIDE}
        current["side"] = [{"key": s["key"], "on": bool(s.get("on"))} for s in values["side"]
                           if isinstance(s, dict) and s.get("key") in known]
    kv_set(conn, LAYOUT_KEY, current)


def _ints(values):
    if not isinstance(values, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        raise DeskError("Zły układ.")
    return values


def snapshot(conn):
    """Obecny układ w formacie save_layout: do cofnięcia zmiany."""
    rows = categories(conn)
    all_tiles = conn.execute(select(desk_tile.c.id, desk_tile.c.category_id).order_by(desk_tile.c.position, desk_tile.c.id)).all()
    out = [c["id"] for c in rows]
    at = look(conn)["stats_at"]
    out.insert(len(out) if not isinstance(at, int) else max(0, min(at, len(out))), "statystyki")
    return {"sections": out, "tiles": {str(c["id"]): [t.id for t in all_tiles if t.category_id == c["id"]] for c in rows},
            "widgets": [w["id"] for w in widgets(conn)]}


def save_layout(conn, data):
    """Zapisuje kolejność: sections = [id kategorii i „statystyki”], tiles = {kategoria: [kafle]}, widgets = [id]."""
    if not isinstance(data, dict):
        raise DeskError("Zły układ.")
    if "sections" in data:
        rows = data["sections"]
        if not isinstance(rows, list) or rows.count("statystyki") != 1:
            raise DeskError("Zły układ.")
        at = rows.index("statystyki")
        for pos, cat in enumerate(_ints([r for r in rows if r != "statystyki"])):
            conn.execute(desk_category.update().where(desk_category.c.id == cat).values(position=pos))
        current = look(conn)
        current["stats_at"] = at
        kv_set(conn, LAYOUT_KEY, current)
    if "tiles" in data:
        if not isinstance(data["tiles"], dict):
            raise DeskError("Zły układ.")
        known = {c["id"] for c in categories(conn)}
        for key, ids in data["tiles"].items():
            if not str(key).isdecimal() or int(key) not in known:
                raise DeskError("Nie ma takiej kategorii.")
            for pos, tile in enumerate(_ints(ids)):
                conn.execute(desk_tile.update().where(desk_tile.c.id == tile).values(category_id=int(key), position=pos))
    if "widgets" in data:
        for pos, widget in enumerate(_ints(data["widgets"])):
            conn.execute(desk_widget.update().where(desk_widget.c.id == widget).values(position=pos))


# ───────────────────────── usuwanie z cofnięciem ─────────────────────────
def _to_trash(conn, kind, row, tile_rows=()):
    token = secrets.token_urlsafe(12)
    entry = {"token": token, "kind": kind, "row": dict(row), "tiles": [dict(t) for t in tile_rows]}
    kv_set(conn, TRASH_KEY, ([entry] + (kv_get(conn, TRASH_KEY) or []))[:TRASH_SIZE])
    return token


def remove(conn, kind, row_id):
    """Usuwa kafel, kategorię (z kafelkami) albo widżet i zostawia kopię do cofnięcia. Zwraca (token, nazwa)."""
    if kind == "ikona":
        row = conn.execute(select(desk_tile).where(desk_tile.c.id == row_id)).mappings().first()
        view = get_tile(conn, row_id)
        if not row:
            raise DeskError("Tej ikony już nie ma.")
        token = _to_trash(conn, kind, row)
        conn.execute(desk_tile.delete().where(desk_tile.c.id == row_id))
        return token, view["name"]
    if kind == "kategoria":
        row = conn.execute(select(desk_category).where(desk_category.c.id == row_id)).mappings().first()
        if not row:
            raise DeskError("Tej kategorii już nie ma.")
        tile_rows = conn.execute(select(desk_tile).where(desk_tile.c.category_id == row_id)).mappings().all()
        token = _to_trash(conn, kind, row, tile_rows)
        conn.execute(desk_category.delete().where(desk_category.c.id == row_id))
        return token, row["name"]
    if kind == "widzet":
        row = conn.execute(select(desk_widget).where(desk_widget.c.id == row_id)).mappings().first()
        if not row:
            raise DeskError("Tej statystyki już nie ma.")
        token = _to_trash(conn, kind, row)
        conn.execute(desk_widget.delete().where(desk_widget.c.id == row_id))
        return token, row["title"] or WIDGETS.get(row["kind"], (row["kind"],))[0]
    raise DeskError("Nie wiem, co usunąć.")


def _restore_tile(conn, row, category_id):
    free = not row["device_id"] or not conn.execute(select(desk_tile.c.id).where(desk_tile.c.device_id == row["device_id"])).first()
    alive = not row["device_id"] or conn.execute(select(device.c.id).where(device.c.id == row["device_id"])).first()
    if free and alive:
        values = {k: v for k, v in row.items() if k != "id"}
        conn.execute(desk_tile.insert().values(**{**values, "category_id": category_id}))


def restore(conn, token):
    """Cofa usunięcie. Kategoria o tej samej nazwie (dodana w międzyczasie) przyjmuje kafelki zamiast dublować się."""
    trash = kv_get(conn, TRASH_KEY) or []
    entry = next((e for e in trash if e["token"] == token), None)
    if not entry:
        raise DeskError("Tego nie da się już cofnąć.")
    row = entry["row"]
    if entry["kind"] == "ikona":
        exists = conn.execute(select(desk_category.c.id).where(desk_category.c.id == row["category_id"])).first()
        if not exists:
            raise DeskError("Kategorii tej ikony już nie ma.")
        _restore_tile(conn, row, row["category_id"])
    elif entry["kind"] == "kategoria":
        found = _named(conn, row["name"])
        cat = found["id"] if found else conn.execute(desk_category.insert().values(
            name=row["name"], position=row["position"])).inserted_primary_key[0]
        for t in entry["tiles"]:
            _restore_tile(conn, t, cat)
    else:
        conn.execute(desk_widget.insert().values(**{k: v for k, v in row.items() if k != "id"}))
    kv_set(conn, TRASH_KEY, [e for e in trash if e["token"] != token])


def new_category(conn):
    """Pusta kategoria z wolną nazwą, od razu do przemianowania."""
    used = {c["name"].lower() for c in categories(conn)}
    name = NEW_CATEGORY
    n = 1
    while name.lower() in used:
        n += 1
        name = f"{NEW_CATEGORY} {n}"
    return ensure_category(conn, name)
