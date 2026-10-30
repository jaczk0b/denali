"""Profile: ustawienia na poziomie typu urządzenia.

Wartość ustawienia = domyślna ← profil ← wyjątek na urządzeniu. Urządzenie bez profilu dostaje profil po typie,
więc na pojedynczym urządzeniu nie trzeba ustawiać niczego. Wyjątek równy wartości z profilu jest usuwany —
„inne niż profil” pokazuje tylko prawdziwe różnice.
"""
from sqlalchemy import delete, select

from .models import device, device_override, profile

# klucz → (typ albo dozwolone wartości, wartość domyślna, opis)
SETTINGS = {
    "monitor": (bool, True, "Monitoruj stan"),
    "down_after_s": (int, 120, "Po ilu sekundach niedziałania zgłosić awarię"),
    "notify": (bool, True, "Powiadamiaj na Discordzie"),
    "updates": (("powiadom", "ignoruj"), "powiadom", "Dostępne aktualizacje"),
    "actions": (list, [], "Dozwolone akcje"),
}
DEFAULT_PROFILE = "Domyślny"


class ProfileError(ValueError):
    pass


def validate(key, value):
    if key not in SETTINGS:
        raise ProfileError(f"Nieznane ustawienie: {key}")
    typ, _default, label = SETTINGS[key]
    if isinstance(typ, tuple):
        ok = value in typ
    elif typ is int:
        ok = isinstance(value, int) and not isinstance(value, bool) and 10 <= value <= 86400
    elif typ is list:
        ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
    else:
        ok = isinstance(value, typ)
    if not ok:
        raise ProfileError(f"Zła wartość dla „{label}”.")
    return sorted(set(value)) if typ is list else value


def profile_for(conn, dev):
    """Profil urządzenia: wybrany ręcznie albo pierwszy pasujący po typie, w ostateczności „Domyślny”."""
    rows = conn.execute(select(profile).order_by(profile.c.id)).mappings().all()
    if dev["profile_id"] is not None:
        for p in rows:
            if p["id"] == dev["profile_id"]:
                return p
    for p in rows:
        if dev["kind"] in (p["kinds"] or []):
            return p
    return next((p for p in rows if p["name"] == DEFAULT_PROFILE), None)


def effective(conn, device_id):
    """{klucz: {"value", "source": domyslne | profil | urzadzenie, "profile_value"}} + nazwa profilu."""
    dev = conn.execute(select(device).where(device.c.id == device_id)).mappings().first()
    prof = profile_for(conn, dev)
    psettings = (prof["settings"] if prof else None) or {}
    overrides = {r.key: r.value for r in conn.execute(
        select(device_override).where(device_override.c.device_id == device_id))}
    out = {}
    for key, (_t, default, _l) in SETTINGS.items():
        pval, src = (psettings[key], "profil") if key in psettings else (default, "domyslne")
        if key in overrides:
            out[key] = {"value": overrides[key], "source": "urzadzenie", "profile_value": pval}
        else:
            out[key] = {"value": pval, "source": src, "profile_value": pval}
    return prof["name"] if prof else None, out


def value(conn, device_id, key):
    return effective(conn, device_id)[1][key]["value"]


def set_override(conn, device_id, key, val):
    val = validate(key, val)
    clear_override(conn, device_id, key)
    _name, eff = effective(conn, device_id)
    if eff[key]["profile_value"] != val:
        conn.execute(device_override.insert().values(device_id=device_id, key=key, value=val))


def clear_override(conn, device_id, key):
    conn.execute(delete(device_override).where(device_override.c.device_id == device_id, device_override.c.key == key))


def set_profile_setting(conn, profile_id, key, val):
    """Zmiana w profilu dotyczy wszystkich urządzeń z tym profilem (poza ich świadomymi wyjątkami)."""
    p = conn.execute(select(profile).where(profile.c.id == profile_id)).mappings().first()
    if not p:
        raise ProfileError("Nie ma takiego profilu.")
    settings = dict(p["settings"] or {})
    val = validate(key, val)
    if val == SETTINGS[key][1]:
        settings.pop(key, None)
    else:
        settings[key] = val
    conn.execute(profile.update().where(profile.c.id == profile_id).values(settings=settings))
