"""Jedna lista aktualizacji ze wszystkich źródeł: pakiety węzłów Proxmoxa i PBS, nowsze obrazy stacków Komodo,
pakiety i „wymagany restart” z agenta na maszynach.

Lista pokazuje tylko obiekty przypięte do urządzeń. Profil „Dostępne aktualizacje: ignoruj” chowa urządzenie
z licznika (zostaje na liście, wyszarzone) — np. maszyna testowa, która nikogo nie obchodzi.
"""
from sqlalchemy import select

from . import profiles
from .models import device, external_ref, integration, observed_state, update_item


def _items(conn, device_id=None):
    q = (select(update_item, external_ref.c.device_id, integration.c.name.label("source"),
                integration.c.type.label("source_type"), integration.c.last_ok_at, integration.c.last_error)
         .join(external_ref, (external_ref.c.integration_id == update_item.c.integration_id)
               & (external_ref.c.external_id == update_item.c.external_id))
         .join(integration, integration.c.id == update_item.c.integration_id)
         .where(external_ref.c.status == "zatwierdzony", external_ref.c.device_id.isnot(None))
         .order_by(update_item.c.security.desc(), update_item.c.component))
    if device_id is not None:
        q = q.where(external_ref.c.device_id == device_id)
    return conn.execute(q).mappings().all()


def reboot_required(conn, device_id=None):
    """{device_id: [nazwa źródła]} — urządzenia, które zgłaszają „wymagany restart” (agent)."""
    q = (select(external_ref.c.device_id, observed_state.c.data, integration.c.name)
         .join(observed_state, (observed_state.c.integration_id == external_ref.c.integration_id)
               & (observed_state.c.external_id == external_ref.c.external_id))
         .join(integration, integration.c.id == external_ref.c.integration_id)
         .where(external_ref.c.status == "zatwierdzony", external_ref.c.device_id.isnot(None)))
    if device_id is not None:
        q = q.where(external_ref.c.device_id == device_id)
    out = {}
    for r in conn.execute(q):
        if (r.data or {}).get("reboot_required"):
            out.setdefault(r.device_id, []).append(r.name)
    return out


def overview(conn):
    """Urządzenia z aktualizacjami albo wymaganym restartem: najpierw ważne (bezpieczeństwo), ignorowane na końcu."""
    items, reboot = _items(conn), reboot_required(conn)
    ids = {i["device_id"] for i in items} | set(reboot)
    if not ids:
        return []
    names = {d.id: d for d in conn.execute(select(device.c.id, device.c.name, device.c.kind)
                                           .where(device.c.id.in_(ids)))}
    out = []
    for did in ids:
        own = [i for i in items if i["device_id"] == did]
        out.append({
            "device": names[did], "rows": own, "security": sum(1 for i in own if i["security"]),
            "sources": sorted({i["source"] for i in own} | set(reboot.get(did, []))),
            "reboot": bool(reboot.get(did)), "ignored": profiles.value(conn, did, "updates") == "ignoruj",
            "since": min((i["detected_at"] for i in own), default=None),
        })
    out.sort(key=lambda r: (r["ignored"], -r["security"], not r["reboot"], -len(r["rows"]), r["device"].name.lower()))
    return out


def for_device(conn, device_id):
    return {"rows": _items(conn, device_id), "reboot": reboot_required(conn, device_id).get(device_id, [])}


def count(conn):
    """Ile urządzeń czeka na aktualizację (bez ignorowanych) — licznik w menu."""
    return sum(1 for r in overview(conn) if not r["ignored"])


def version_text(item):
    if item["kind"] == "obraz":
        return "nowszy obraz" + (f" · {item['current']}" if item["current"] else "")
    if item["current"] and item["available"]:
        return f"{item['current']} → {item['available']}"
    return item["available"] or "nowsza wersja"
