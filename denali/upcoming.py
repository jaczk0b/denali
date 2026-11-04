"""„Nadchodzące” na pulpicie: jedna lista terminów, które dziś są rozrzucone po kartach.

- certyfikaty HTTPS, które wygasają w ciągu CERT_DAYS (ze sprawdzeń HTTP),
- usługi OVH opłacone do daty w ciągu PAY_DAYS (z integracji OVHcloud),
- aktualizacje bezpieczeństwa czekające dłużej niż SECURITY_WAIT_DAYS,
- kopia zapasowa Denali starsza niż BACKUP_DAYS,
- magazyny (pule TrueNAS, PBS), które według prognozy zapełnią się w ciągu forecast.SHOW_DAYS.
Najbliższe (albo już zaległe) na górze. Nic nie trzeba ustawiać — wszystko liczy się z danych, które już są.
"""
import time
from datetime import datetime

from sqlalchemy import select

from . import forecast
from .db import kv_get
from .models import check_state, device, external_ref, integration, observed_state, update_item

CERT_DAYS = 30
PAY_DAYS = 30
SECURITY_WAIT_DAYS = 7
BACKUP_DAYS = 2
LIMIT = 8


def _days(seconds):
    d = round(seconds / 86400)
    if d == 0:
        return "dziś"
    if d == 1:
        return "jutro"
    if d == -1:
        return "wczoraj"
    return f"za {d} dni" if d > 0 else f"{-d} dni temu"


def items(conn, now=None):
    now = now or time.time()
    names = {r.id: r.name for r in conn.execute(select(device.c.id, device.c.name))}
    out = []
    seen_certs = set()
    for c in conn.execute(select(check_state).where(check_state.c.cert_expires_at.isnot(None),
                                                    check_state.c.cert_expires_at < now + CERT_DAYS * 86400)
                          ).mappings():
        key = (c["device_id"], c["target"].split("/")[2] if "//" in c["target"] else c["target"])
        if key in seen_certs:
            continue
        seen_certs.add(key)
        left = c["cert_expires_at"] - now
        out.append({"when": c["cert_expires_at"], "level": "down" if left < 7 * 86400 else "warn",
                    "text": f"Certyfikat {key[1]} wygasa {_days(left)}" if left > 0 else f"Certyfikat {key[1]} wygasł",
                    "link": f"/urzadzenia/{c['device_id']}"})
    refs = {(r.integration_id, r.external_id): r.device_id for r in conn.execute(
        select(external_ref.c.integration_id, external_ref.c.external_id, external_ref.c.device_id)
        .where(external_ref.c.device_id.isnot(None)))}
    for s in conn.execute(select(observed_state.c.integration_id, observed_state.c.external_id, observed_state.c.data)
                          .join(integration, integration.c.id == observed_state.c.integration_id)
                          .where(integration.c.type == "ovh")).mappings():
        exp = (s["data"] or {}).get("expiration")
        try:
            ts = datetime.strptime(exp, "%Y-%m-%d").timestamp() if exp else None
        except ValueError:
            ts = None
        if not ts or ts > now + PAY_DAYS * 86400:
            continue
        dev = refs.get((s["integration_id"], s["external_id"]))
        label = names.get(dev, s["external_id"])
        out.append({"when": ts, "level": "down" if ts < now + 7 * 86400 else "warn",
                    "text": f"Opłata {label} kończy się {_days(ts - now)} ({exp})",
                    "link": f"/urzadzenia/{dev}" if dev else "/infrastruktura/vps"})
    waiting = {}
    for u in conn.execute(select(update_item.c.integration_id, update_item.c.external_id, update_item.c.detected_at)
                          .where(update_item.c.security.is_(True),
                                 update_item.c.detected_at < now - SECURITY_WAIT_DAYS * 86400)):
        dev = refs.get((u.integration_id, u.external_id))
        if dev:
            n, oldest = waiting.get(dev, (0, now))
            waiting[dev] = (n + 1, min(oldest, u.detected_at))
    for dev, (n, oldest) in waiting.items():
        out.append({"when": oldest, "level": "warn", "link": f"/urzadzenia/{dev}",
                    "text": f"{names.get(dev, dev)}: {n} aktualizacji bezpieczeństwa czeka od {round((now - oldest) / 86400)} dni"})
    last_backup = kv_get(conn, "backup.last_ok")
    if last_backup and now - last_backup > BACKUP_DAYS * 86400:
        out.append({"when": last_backup, "level": "warn", "link": "/admin/audyt",
                    "text": f"Ostatnia kopia Denali {_days(last_backup - now)} — sprawdź workera"})
    for f in forecast.forecasts(conn, now):
        if f["days"] is not None and f["days"] < forecast.SHOW_DAYS:
            out.append({"when": now + f["days"] * 86400, "level": "down" if f["days"] < forecast.ALERT_DAYS else "warn",
                        "text": forecast.describe(f), "link": f"/urzadzenia/{f['device_id']}" if f["device_id"] else "/"})
    out.sort(key=lambda x: x["when"])
    return out[:LIMIT]
