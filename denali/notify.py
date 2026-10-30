"""Powiadomienia o awariach (Discord).

- jedno powiadomienie na incydent, o przyczynie — skutki są w treści, bez osobnych wiadomości,
- wysyłane dopiero GROUP_WAIT po wykryciu, żeby zebrać skutki (pomysł group_wait z HLC),
- „rozwiązane”, gdy incydent zamknie się sam albo ręcznie (tylko jeśli wcześniej poszło powiadomienie),
- „Zajmuję się tym” przed wysłaniem — powiadomienie pomijane; profil „Powiadamiaj: nie” — tak samo,
- nieudana wysyłka zostanie ponowiona w następnym obiegu workera.
"""
import json
import logging
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from . import incidents, profiles
from .models import incident, notification_channel

log = logging.getLogger("denali.notify")
GROUP_WAIT = 60
SEVERITY_RANK = {"info": 0, "ostrzezenie": 1, "krytyczny": 2}
SEVERITY_LABEL = {"info": "informacja", "ostrzezenie": "ostrzeżenie", "krytyczny": "krytyczny"}
COLORS = {"krytyczny": 0xFF6B6B, "ostrzezenie": 0xF2A93B, "info": 0x9BA5B0, "ok": 0x45C486}


class NotifyError(ValueError):
    pass


def validate_webhook(url):
    u = urlsplit((url or "").strip())
    if u.scheme != "https" or u.hostname not in ("discord.com", "discordapp.com", "ptb.discord.com",
                                                  "canary.discord.com") or not u.path.startswith("/api/webhooks/"):
        raise NotifyError("To nie jest adres webhooka Discorda (https://discord.com/api/webhooks/…).")
    return url.strip()


def save_channel(conn, box, name, webhook, min_severity="ostrzezenie", channel_id=None):
    if min_severity not in SEVERITY_RANK:
        raise NotifyError("Nieznany próg ważności.")
    name = (name or "").strip() or "Discord"
    values = {"type": "discord", "name": name[:100], "min_severity": min_severity}
    if webhook or channel_id is None:
        values["config_ct"] = box.encrypt(json.dumps({"webhook": validate_webhook(webhook)}))
    if channel_id is None:
        return conn.execute(notification_channel.insert().values(**values)).inserted_primary_key[0]
    conn.execute(notification_channel.update().where(notification_channel.c.id == channel_id).values(**values))
    return channel_id


def channels(conn, enabled_only=False):
    q = select(notification_channel).order_by(notification_channel.c.name)
    if enabled_only:
        q = q.where(notification_channel.c.enabled.is_(True))
    return conn.execute(q).mappings().all()


# ───────────────────────── treść ─────────────────────────
def message(inc, base_url, resolved=False):
    names = {r: [d["name"] for d in inc["devices"] if d["role"] == r] for r in ("przyczyna", "skutek", "gorzej")}
    url = f"{base_url.rstrip('/')}/incydenty/{inc['id']}"
    if resolved:
        title = f"Rozwiązane: {inc['title']}"
        lines = [f"Trwało od {_hm(inc['started_at'])} do {_hm(inc['ended_at'])}."]
        color = COLORS["ok"]
    else:
        title = inc["title"]
        lines = [f"**Przyczyna:** {', '.join(names['przyczyna']) or '—'}"]
        if names["skutek"]:
            lines.append(f"**Przez to nie działa:** {', '.join(names['skutek'])} — to skutek, nie osobne awarie")
        if names["gorzej"]:
            lines.append(f"**Działa gorzej:** {', '.join(names['gorzej'])}")
        evidence = [e["text"] for e in inc["events"] if e["kind"] == "dowod"]
        if evidence:
            lines.append("_" + " ".join(evidence[:2]) + "_")
        lines.append(f"Waga: {SEVERITY_LABEL.get(inc['severity'], inc['severity'])} · od {_hm(inc['started_at'])}")
        color = COLORS.get(inc["severity"], COLORS["ostrzezenie"])
    return {"username": "Denali", "allowed_mentions": {"parse": []},
            "embeds": [{"title": title[:256], "url": url, "description": "\n".join(lines)[:4000], "color": color}]}


def _hm(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"


async def send(webhook, payload, transport=None):
    kw = {"transport": transport} if transport else {}
    try:
        async with httpx.AsyncClient(timeout=10, **kw) as cli:
            r = await cli.post(webhook, json=payload)
    except httpx.HTTPError as e:
        raise NotifyError(f"Brak połączenia z Discordem ({e.__class__.__name__}).") from None
    if r.status_code == 429:
        raise NotifyError("Discord prosi o wolniejsze wysyłanie — spróbuję ponownie.")
    if r.status_code in (401, 403, 404):
        raise NotifyError("Discord odrzucił webhook — mógł zostać usunięty. Dodaj go ponownie.")
    if r.status_code >= 400:
        raise NotifyError(f"Discord zwrócił błąd {r.status_code}.")


async def deliver(conn, box, payload, severity, transport=None, now=None):
    """Wysyła do wszystkich włączonych kanałów z odpowiednim progiem. True, gdy choć jeden przyjął."""
    now = now or time.time()
    delivered = False
    for ch in channels(conn, enabled_only=True):
        if SEVERITY_RANK.get(severity, 1) < SEVERITY_RANK.get(ch["min_severity"], 1):
            continue
        try:
            await send(json.loads(box.decrypt(ch["config_ct"]))["webhook"], payload, transport)
        except (NotifyError, ValueError) as e:
            conn.execute(notification_channel.update().where(notification_channel.c.id == ch["id"]).values(
                last_error=str(e)[:300]))
            continue
        conn.execute(notification_channel.update().where(notification_channel.c.id == ch["id"]).values(
            last_sent_at=now, last_error=""))
        delivered = True
    return delivered


# ───────────────────────── obieg workera ─────────────────────────
async def process(conn, box, base_url, transport=None, now=None):
    """Wysyła zaległe powiadomienia. Zwraca liczbę wysłanych wiadomości."""
    now = now or time.time()
    if not channels(conn, enabled_only=True):
        return 0
    sent = 0
    pending = conn.execute(select(incident).where(
        incident.c.source == "auto", incident.c.notified_at.is_(None))).mappings().all()
    for row in pending:
        if row["status"] != "trwa" or not row["root_device_id"] \
                or not profiles.value(conn, row["root_device_id"], "notify"):
            # zamknięty przed wysyłką, ktoś już się zajmuje albo powiadomienia wyłączone w profilu
            conn.execute(incident.update().where(incident.c.id == row["id"]).values(notified_at=now,
                                                                                   closed_notified_at=now))
            continue
        if now - row["started_at"] < GROUP_WAIT:
            continue
        inc = incidents.get_incident(conn, row["id"])
        if await deliver(conn, box, message(inc, base_url), inc["severity"], transport, now):
            conn.execute(incident.update().where(incident.c.id == row["id"]).values(notified_at=now))
            sent += 1
    resolved = conn.execute(select(incident).where(
        incident.c.status == "zamkniety", incident.c.notified_at.isnot(None),
        incident.c.closed_notified_at.is_(None))).mappings().all()
    for row in resolved:
        inc = incidents.get_incident(conn, row["id"])
        if await deliver(conn, box, message(inc, base_url, resolved=True), inc["severity"], transport, now):
            conn.execute(incident.update().where(incident.c.id == row["id"]).values(closed_notified_at=now))
            sent += 1
    return sent


async def test_channel(conn, box, channel_id, base_url, transport=None):
    ch = conn.execute(select(notification_channel).where(notification_channel.c.id == channel_id)).mappings().first()
    if not ch:
        raise NotifyError("Nie ma takiego kanału.")
    payload = {"username": "Denali", "allowed_mentions": {"parse": []},
               "embeds": [{"title": "Test powiadomień Denali", "url": base_url,
                           "description": "Jeśli to widzisz, awarie trafią na ten kanał.", "color": COLORS["ok"]}]}
    await send(json.loads(box.decrypt(ch["config_ct"]))["webhook"], payload, transport)
