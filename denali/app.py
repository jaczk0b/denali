"""Aplikacja webowa (FastAPI)."""
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import __version__
from .config import Settings, get_settings
from .crypto import Box
from .db import kv_get, make_engine
from .templating import render
from .web import admin_routes, auth_routes, docs_routes, infra_routes, integration_routes, notify_routes
from .web.deps import CsrfError, Forbidden, NotFound, Redirect

WORKER_LATE_AFTER = 120  # s bez sygnału życia = worker nie działa
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.check()
    app = FastAPI(title="Denali", version=__version__,
                  docs_url="/docs" if settings.dev else None, redoc_url=None,
                  openapi_url="/openapi.json" if settings.dev else None)
    app.state.settings = settings
    app.state.engine = make_engine(settings.db_url)
    # tryb deweloperski bez klucza: klucz tymczasowy (zaszyfrowane dane nie przeżyją restartu)
    app.state.box = Box(settings.encryption_key or Fernet.generate_key().decode())

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if not request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return RedirectResponse(exc.url, 303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return render(request, "blad.html", {"title": "Brak dostępu", "text": "Ta strona jest tylko dla administratora."}, 403)

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, exc: CsrfError):
        return render(request, "blad.html", {"title": "Formularz wygasł",
                                             "text": "Odśwież stronę i spróbuj jeszcze raz."}, 400)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return render(request, "blad.html", {"title": "Nie znaleziono", "text": exc.text}, 404)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for r in (auth_routes, infra_routes, docs_routes, admin_routes, integration_routes, notify_routes):
        app.include_router(r.router)

    @app.get("/health")
    def health():
        """Stan samego panelu: baza i worker. Zawsze 200, gdy baza odpowiada — healthcheck kontenera nie zależy od workera."""
        with app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            beat = kv_get(conn, "worker.heartbeat")
        if beat is None:
            worker = "brak"
        elif time.time() - beat > WORKER_LATE_AFTER:
            worker = "opóźniony"
        else:
            worker = "ok"
        return {"status": "ok" if worker == "ok" else "uwaga", "version": __version__, "db": "ok", "worker": worker}

    return app
