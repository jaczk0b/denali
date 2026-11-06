"""Worker: zadania w tle — odczyt integracji, eksport awaryjny, kopia zapasowa. Osobny proces, żeby jego
restart nie wyłączał panelu."""
import asyncio
import logging
import time

from . import (actions, agents, alerts, backup, changes, checks, export, forecast, history, integrations, live, notify,
               oui, uplink, watchdog, weekly)
from .config import Settings
from .crypto import Box
from .db import kv_get, kv_set, make_engine

log = logging.getLogger("denali.worker")
EXPORT_FORCE_EVERY = 3600   # eksport awaryjny: po każdej zmianie, a co godzinę i tak (świeża data na stronie)
BACKUP_EVERY = 24 * 3600    # kopia bazy i plików do data/backups (14 ostatnich); PBS i tak kopiuje całą VM
CHECKS_EVERY = 60           # ping i HTTP z LAN
PRUNE_EVERY = 3600          # stara historia stanów i czasów odpowiedzi
FORECAST_EVERY = 900        # „kończy się miejsce” i poniedziałkowe podsumowanie tygodnia


def sending(engine):
    """Połączenie do kroków, które czekają na sieć (Discord, SMTP, strażnik): każdy zapis od razu zatwierdzony.
    W transakcji SQLite trzymałby blokadę zapisu przez całą wysyłkę — panel czekałby albo dostał „database is locked”,
    a błąd w dalszym kroku cofnąłby „wysłano” i wiadomość wyszłaby drugi raz."""
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def tick(engine, settings: Settings, state: dict, now: float | None = None) -> None:
    now = now or time.time()
    with engine.begin() as conn:
        kv_set(conn, "worker.heartbeat", now)
    try:
        with engine.connect() as conn:
            force = now - state.get("export_forced_at", 0) >= EXPORT_FORCE_EVERY
            export.run(conn, settings.data_dir, force=force, now=now)
            if force:
                state["export_forced_at"] = now
        with engine.begin() as conn:
            kv_set(conn, "export.last_ok", now)
    except Exception:   # błąd eksportu nie może zatrzymać workera
        log.exception("Eksport statyczny nie powiódł się")
    if now - state.get("pruned_at", 0) >= PRUNE_EVERY:
        state["pruned_at"] = now
        try:
            with engine.begin() as conn:
                history.prune(conn, now)
                forecast.prune(conn, now)
                alerts.prune(conn, now)
                changes.prune(conn, now)
        except Exception:
            log.exception("Czyszczenie historii nie powiodło się")
    with engine.connect() as conn:
        last_backup = kv_get(conn, "backup.last_ok", 0)
    if now - last_backup >= BACKUP_EVERY:
        try:
            path = backup.create(settings, now=now)
            backup.prune(backup.backups_dir(settings.data_dir))
            with engine.begin() as conn:
                kv_set(conn, "backup.last_ok", now)
            log.info("Kopia zapasowa: %s", path)
        except Exception:
            log.exception("Kopia zapasowa nie powiodła się")


async def run(settings: Settings, stop: asyncio.Event | None = None, interval: float = 30) -> None:
    stop = stop or asyncio.Event()
    engine = make_engine(settings.db_url)
    box = Box(settings.encryption_key) if settings.encryption_key else None
    state: dict = {}
    polls: dict = {}            # odczyty integracji w toku: {id integracji: zadanie}
    log.info("worker start")
    try:
        while not stop.is_set():
            if box:
                try:
                    integrations.start_due(engine, box, polls)
                except Exception:   # błąd odczytu integracji nie może zatrzymać workera
                    log.exception("Odczyt integracji nie powiódł się")
                try:
                    await actions.follow(engine, box)
                except Exception:   # błąd przy dokańczaniu akcji nie może zatrzymać workera
                    log.exception("Dokończenie akcji nie powiodło się")
            if time.time() - state.get("checks_at", 0) >= CHECKS_EVERY:
                state["checks_at"] = time.time()
                try:
                    await checks.run(engine)
                except Exception:
                    log.exception("Sprawdzenia nie powiodły się")
            try:
                online = await uplink.online()
                with engine.begin() as conn:
                    uplink.record(conn, online)
            except Exception:
                log.exception("Sprawdzenie internetu nie powiodło się")
            try:
                with engine.begin() as conn:
                    agents.mark_silent(conn)
                    live.evaluate(conn)
                    alerts.evaluate(conn)
            except Exception:
                log.exception("Ocena stanu i incydentów nie powiodła się")
            if not state.get("oui_task") or state["oui_task"].done():
                state["oui_task"] = asyncio.create_task(oui.refresh(settings.data_dir, state))
            if box:
                try:
                    with sending(engine) as conn:
                        await notify.process(conn, box, settings.base_url)
                        await notify.process_alerts(conn, box, settings.base_url, live.saved(conn))
                        await notify.reminders(conn, box, settings.base_url)
                except Exception:
                    log.exception("Wysyłka powiadomień nie powiodła się")
                if time.time() - state.get("forecast_at", 0) >= FORECAST_EVERY:
                    state["forecast_at"] = time.time()
                    try:
                        with sending(engine) as conn:
                            await notify.forecast_alerts(conn, box, settings.base_url)
                            await weekly.process(conn, box, settings.base_url)
                            await weekly.process_month(conn, box, settings.base_url)
                    except Exception:
                        log.exception("Prognoza miejsca albo podsumowanie tygodnia nie powiodły się")
                try:
                    with sending(engine) as conn:
                        await watchdog.ping(conn, box)
                except Exception:
                    log.exception("Meldunek strażnika nie powiódł się")
            await asyncio.to_thread(tick, engine, settings, state)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        for task in polls.values():
            task.cancel()
        if state.get("oui_task"):
            state["oui_task"].cancel()
        await asyncio.gather(*polls.values(), return_exceptions=True)
        engine.dispose()
        log.info("worker stop")
