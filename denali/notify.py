"""Powiadomienia o awariach: Discord, ntfy, Telegram, e-mail.

- jedno powiadomienie na incydent, o przyczynie — skutki są w treści, bez osobnych wiadomości,
- wysyłane dopiero GROUP_WAIT po wykryciu, żeby zebrać skutki (pomysł group_wait z HLC),
- „rozwiązane”, gdy incydent zamknie się sam albo ręcznie (tylko jeśli wcześniej poszło powiadomienie),
- „Zajmuję się tym” przed wysłaniem — powiadomienie pomijane; profil „Powiadamiaj: nie” — tak samo,
- nieudana wysyłka zostanie ponowiona w następnym obiegu workera,
- krytyczny incydent bez reakcji: powtórka co N godzin, gdy jest ustawiona,
- alerty (alerts.py) idą zbiorczo: jedna wiadomość na wagę, potem jedna „rozwiązane”.
Treść powstaje w formacie Discorda; pozostałe kanały dostają z niej zwykły tekst.
"""
import asyncio
import json
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from . import alerts, forecast, incidents, profiles
from .db import kv_get, kv_set
from .models import incident, notification_channel

log = logging.getLogger("denali.notify")
GROUP_WAIT = 60
SEVERITY_RANK = {"info": 0, "ostrzezenie": 1, "krytyczny": 2}
SEVERITY_LABEL = {"info": "informacja", "ostrzezenie": "ostrzeżenie", "krytyczny": "krytyczny"}
CHANNEL_TYPES = {"discord": "Discord", "ntfy": "ntfy", "telegram": "Telegram", "email": "E-mail"}
REPEAT_KEY = "notify.repeat_hours"
REPEAT_CHOICES = (0, 1, 4, 12, 24)
ALERTS_IN_MESSAGE = 15
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


def _text_field(form, key, label):
    value = str(form.get(key) or "").strip()
    if not value:
        raise NotifyError(f"Podaj: {label}.")
    return value


def validate_other(type_, form):
    """Ustawienia kanału innego niż Discord → słownik do zaszyfrowania."""
    if type_ == "ntfy":
        url = _text_field(form, "url", "adres serwera ntfy").rstrip("/")
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise NotifyError("Adres ntfy musi zaczynać się od https:// (albo http:// w LAN).")
        topic = _text_field(form, "topic", "temat")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise NotifyError("Temat ntfy: litery, cyfry, - i _.")
        return {"url": url, "topic": topic, "token": str(form.get("token") or "").strip()}
    if type_ == "telegram":
        token = _text_field(form, "token", "token bota")
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise NotifyError("To nie wygląda na token bota Telegrama (123456:ABC…).")
        return {"token": token, "chat_id": _text_field(form, "chat_id", "chat ID")}
    if type_ == "email":
        port = str(form.get("port") or "587").strip()
        if not port.isdigit():
            raise NotifyError("Port SMTP to liczba (587 albo 465).")
        return {"host": _text_field(form, "host", "serwer SMTP"), "port": int(port), "user": str(form.get("user") or "").strip(),
                "password": str(form.get("password") or ""), "sender": _text_field(form, "sender", "nadawcę"),
                "to": _text_field(form, "to", "adresata")}
    raise NotifyError("Nieznany rodzaj kanału.")


def save_other(conn, box, type_, name, form, min_severity="ostrzezenie"):
    if min_severity not in SEVERITY_RANK:
        raise NotifyError("Nieznany próg ważności.")
    config = validate_other(type_, form)
    return conn.execute(notification_channel.insert().values(
        type=type_, name=(name or "").strip()[:100] or CHANNEL_TYPES[type_], min_severity=min_severity,
        config_ct=box.encrypt(json.dumps(config)))).inserted_primary_key[0]


def channels(conn, enabled_only=False):
    q = select(notification_channel).order_by(notification_channel.c.name)
    if enabled_only:
        q = q.where(notification_channel.c.enabled.is_(True))
    return conn.execute(q).mappings().all()


# ───────────────────────── treść ─────────────────────────
def message(inc, base_url, resolved=False):
    """Treść ma odpowiadać na „co się stało”: objaw ze źródła (ping, API, HTTP), skutki i co już wykluczono."""
    names = {r: [d["name"] for d in inc["devices"] if d["role"] == r] for r in ("przyczyna", "skutek", "gorzej")}
    url = f"{base_url.rstrip('/')}/incydenty/{inc['id']}"
    symptoms = [e["text"] for e in inc["events"] if e["kind"] == "objaw"]
    cause = ", ".join(names["przyczyna"]) or "—"
    if resolved:
        title = f"Rozwiązane: {cause} znowu działa" if inc["source"] == "auto" else f"Rozwiązane: {inc['title']}"
        lines = []
        if symptoms:
            lines.append(f"**Było:** {symptoms[0]}")
        closing = [e["text"] for e in inc["events"] if e["kind"] == "zamkniety"]
        if closing and closing[-1].startswith("Znowu działa: "):
            lines.append(f"**Teraz:** {closing[-1].removeprefix('Znowu działa: ')}")
        if names["skutek"]:
            lines.append(f"**Wróciło też:** {', '.join(names['skutek'])}")
        unstable = [e["text"] for e in inc["events"] if e["kind"] == "niestabilne"]
        if unstable:
            lines.append(f"_{unstable[0].split('.')[0]} — warto sprawdzić, dlaczego._")
        lines.append(f"Trwało {_duration(inc['started_at'], inc['ended_at'])} "
                     f"({_hm(inc['started_at'])}–{_hm(inc['ended_at'])}).")
        color = COLORS["ok"]
    else:
        title = inc["title"]
        lines = [f"**Co się stało:** {symptoms[0]}" if symptoms else f"**Przyczyna:** {cause}"]
        if names["skutek"]:
            lines.append(f"**Przez to nie działa:** {', '.join(names['skutek'])} — to skutek, nie osobne awarie")
        if names["gorzej"]:
            lines.append(f"**Działa gorzej:** {', '.join(names['gorzej'])}")
        before = [e["text"] for e in inc["events"] if e["kind"] == "zmiana"]
        if before:
            lines.append("**Wcześniej:** " + "; ".join(before[:3]))
        evidence = [e["text"] for e in inc["events"] if e["kind"] == "dowod"]
        if evidence:
            lines.append("_" + " ".join(evidence[:2]) + "_")
        lines.append(f"Od {_hm(inc['started_at'])} · waga: {SEVERITY_LABEL.get(inc['severity'], inc['severity'])}")
        color = COLORS.get(inc["severity"], COLORS["ostrzezenie"])
    return {"username": "Denali", "allowed_mentions": {"parse": []},
            "embeds": [{"title": title[:256], "url": url, "description": "\n".join(lines)[:4000], "color": color}]}


def _hm(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"


def _duration(start, end):
    if not start or not end:
        return "—"
    minutes = max(1, round((end - start) / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} h"


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


def plain(payload):
    """(tytuł, tekst) z wiadomości Discorda, dla kanałów bez formatowania."""
    embed = payload["embeds"][0]
    body = re.sub(r"\*\*|(?<![\w])_|_(?![\w])", "", embed.get("description") or "")
    return embed.get("title") or "Denali", f"{body}\n{embed['url']}".strip() if embed.get("url") else body


async def _post(url, what, transport, **kw):
    opts = {"transport": transport} if transport else {}
    try:
        async with httpx.AsyncClient(timeout=10, **opts) as cli:
            r = await cli.post(url, **kw)
    except httpx.HTTPError as e:
        raise NotifyError(f"Brak połączenia: {what} ({e.__class__.__name__}).") from None
    if r.status_code >= 400:
        raise NotifyError(f"{what} zwrócił błąd {r.status_code}.")


def _smtp_send(cfg, subject, body):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["sender"], cfg["to"]
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if int(cfg["port"]) == 465:
            server = smtplib.SMTP_SSL(cfg["host"], 465, timeout=15, context=context)
        else:
            server = smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=15)
            server.starttls(context=context)
        with server:
            if cfg.get("user"):
                server.login(cfg["user"], cfg.get("password") or "")
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise NotifyError(f"Serwer poczty odrzucił wiadomość ({e.__class__.__name__}).") from None


async def send_channel(type_, cfg, payload, transport=None):
    if type_ == "discord":
        return await send(cfg["webhook"], payload, transport)
    title, body = plain(payload)
    if type_ == "ntfy":
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else None
        return await _post(cfg["url"], "ntfy", transport, headers=headers,
                           json={"topic": cfg["topic"], "title": title, "message": body})
    if type_ == "telegram":
        return await _post(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", "Telegram", transport,
                           json={"chat_id": cfg["chat_id"], "text": f"{title}\n{body}"})
    if type_ == "email":
        return await asyncio.to_thread(_smtp_send, cfg, f"[Denali] {title}", body)
    raise NotifyError("Nieznany rodzaj kanału.")


async def deliver(conn, box, payload, severity, transport=None, now=None, any_threshold=False):
    """Wysyła do wszystkich włączonych kanałów z odpowiednim progiem (any_threshold: do wszystkich — raporty).
    True, gdy choć jeden przyjął."""
    now = now or time.time()
    delivered = False
    for ch in channels(conn, enabled_only=True):
        if not any_threshold and SEVERITY_RANK.get(severity, 1) < SEVERITY_RANK.get(ch["min_severity"], 1):
            continue
        try:
            await send_channel(ch["type"], json.loads(box.decrypt(ch["config_ct"])), payload, transport)
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


def _lines(rows):
    out = [f"• **{r['device'] or 'Lokalizacja'}:** {r['message']}" for r in rows[:ALERTS_IN_MESSAGE]]
    if len(rows) > ALERTS_IN_MESSAGE:
        out.append(f"…i jeszcze {len(rows) - ALERTS_IN_MESSAGE}")
    return out


def alert_message(rows, severity, base_url, resolved=False):
    url = base_url.rstrip("/") + "/alerty"
    if resolved:
        lines = []
        for r in rows[:ALERTS_IN_MESSAGE]:
            if r["rule"] == "internet_down":
                lines.append(f"• Brak internetu w domu: {_hm(r['first_seen'])}–{_hm(r['resolved_at'])} "
                             f"({_duration(r['first_seen'], r['resolved_at'])})")
            else:
                lines.append(f"• **{r['device'] or 'Lokalizacja'}:** {r['message']}")
        title, color = "Rozwiązane alerty" if len(rows) > 1 else "Rozwiązany alert", COLORS["ok"]
    else:
        lines = _lines(rows)
        title = "Wymaga uwagi" if severity == "ostrzezenie" else "Pilne: wymaga reakcji"
        color = COLORS.get(severity, COLORS["ostrzezenie"])
    return {"username": "Denali", "allowed_mentions": {"parse": []},
            "embeds": [{"title": title, "url": url, "description": "\n".join(lines)[:4000], "color": color}]}


async def process_alerts(conn, box, base_url, live_states, transport=None, now=None):
    """Zbiorcze powiadomienia o alertach. Zwraca liczbę wysłanych wiadomości."""
    now = now or time.time()
    if not channels(conn, enabled_only=True):
        return 0
    sent = 0
    due, skip = alerts.to_notify(conn, live_states, now)
    alerts.mark_notified(conn, [r["id"] for r in skip], now, silently=True)
    for severity in ("krytyczny", "ostrzezenie"):
        rows = [r for r in due if r["severity"] == severity]
        if rows and await deliver(conn, box, alert_message(rows, severity, base_url), severity, transport, now):
            alerts.mark_notified(conn, [r["id"] for r in rows], now)
            sent += 1
    done = alerts.resolved_pending(conn)
    for severity in ("krytyczny", "ostrzezenie"):
        rows = [r for r in done if r["severity"] == severity]
        if rows and await deliver(conn, box, alert_message(rows, severity, base_url, resolved=True), severity, transport, now):
            alerts.mark_resolved_notified(conn, [r["id"] for r in rows], now)
            sent += 1
    return sent


def repeat_hours(conn):
    return int(kv_get(conn, REPEAT_KEY, 0) or 0)


def set_repeat_hours(conn, hours):
    if hours not in REPEAT_CHOICES:
        raise NotifyError("Nieznany odstęp powtórek.")
    kv_set(conn, REPEAT_KEY, hours)


async def reminders(conn, box, base_url, transport=None, now=None):
    """Krytyczny incydent, którym nikt się nie zajął: powtórka co N godzin (0 = wyłączone)."""
    hours = repeat_hours(conn)
    if not hours or not channels(conn, enabled_only=True):
        return 0
    now = now or time.time()
    sent = 0
    for row in conn.execute(select(incident).where(incident.c.source == "auto", incident.c.status == "trwa",
                                                   incident.c.severity == "krytyczny",
                                                   incident.c.notified_at.isnot(None))).mappings().all():
        if now - max(row["notified_at"], row["reminded_at"] or 0) < hours * 3600:
            continue
        payload = message(incidents.get_incident(conn, row["id"]), base_url)
        payload["embeds"][0]["title"] = f"Nadal: {row['title']}"[:256]
        if await deliver(conn, box, payload, "krytyczny", transport, now):
            conn.execute(incident.update().where(incident.c.id == row["id"]).values(reminded_at=now))
            sent += 1
    return sent


async def forecast_alerts(conn, box, base_url, transport=None, now=None):
    """Magazyn zapełni się w ciągu kilkunastu dni — osobna wiadomość, raz na tydzień dla tego samego magazynu."""
    if not channels(conn, enabled_only=True):
        return 0
    due = forecast.alerts_due(conn, now)
    if not due:
        return 0
    payload = {"username": "Denali", "allowed_mentions": {"parse": []},
               "embeds": [{"title": "Kończy się miejsce", "url": base_url.rstrip("/") + "/",
                           "description": "\n".join(forecast.describe(f) for f in due)
                           + "\n_Prognoza z przyrostu w ostatnich dwóch tygodniach._",
                           "color": COLORS["ostrzezenie"]}]}
    if await deliver(conn, box, payload, "ostrzezenie", transport, now):
        forecast.mark_alerted(conn, due, now)
        return 1
    return 0


async def test_channel(conn, box, channel_id, base_url, transport=None):
    ch = conn.execute(select(notification_channel).where(notification_channel.c.id == channel_id)).mappings().first()
    if not ch:
        raise NotifyError("Nie ma takiego kanału.")
    payload = {"username": "Denali", "allowed_mentions": {"parse": []},
               "embeds": [{"title": "Test powiadomień Denali", "url": base_url,
                           "description": "Jeśli to widzisz, awarie trafią na ten kanał.", "color": COLORS["ok"]}]}
    await send_channel(ch["type"], json.loads(box.decrypt(ch["config_ct"])), payload, transport)
