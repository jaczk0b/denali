"""Worker: zadania w tle — odczyt integracji, eksport awaryjny, kopia zapasowa. Osobny proces, żeby jego
restart nie wyłączał panelu."""
import asyncio
import logging
import time

from . import backup, checks, export, integrations, live, notify
from .config import Settings
from .crypto import Box
from .db import kv_get, kv_set, make_engine

log = logging.getLogger("denali.worker")
EXPORT_FORCE_EVERY = 3600   # eksport awaryjny: po każdej zmianie, a co godzinę i tak (świeża data na stronie)
BACKUP_EVERY = 24 * 3600    # kopia bazy i plików do data/backups (14 ostatnich); PBS i tak kopiuje całą VM
CHECKS_EVERY = 60           # ping i HTTP z LAN


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
    log.info("worker start")
    try:
        while not stop.is_set():
            if box:
                try:
                    await integrations.poll_due(engine, box)
                except Exception:   # błąd odczytu integracji nie może zatrzymać workera
                    log.exception("Odczyt integracji nie powiódł się")
            if time.time() - state.get("checks_at", 0) >= CHECKS_EVERY:
                state["checks_at"] = time.time()
                try:
                    await checks.run(engine)
                except Exception:
                    log.exception("Sprawdzenia nie powiodły się")
            try:
                with engine.begin() as conn:
                    live.evaluate(conn)
            except Exception:
                log.exception("Ocena stanu i incydentów nie powiodła się")
            if box:
                try:
                    with engine.begin() as conn:
                        await notify.process(conn, box, settings.base_url)
                except Exception:
                    log.exception("Wysyłka powiadomień nie powiodła się")
            await asyncio.to_thread(tick, engine, settings, state)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        engine.dispose()
        log.info("worker stop")
