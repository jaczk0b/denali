"""Odświeżanie na żywo (SSE): strumień z odciskiem stanu, a przeglądarka (static/denali.js) przy zmianie pobiera
stronę od nowa i podmienia treść. Worker pisze stan do bazy, więc strumień tylko ją czyta — nic tu nie liczymy.

Strumień nie przedłuża sesji (deps.LIVE_PATH) i co kilka sekund sprawdza, czy sesja jeszcze istnieje: wylogowanie,
odebranie tabletu albo 12 h bezczynności zamykają go zdarzeniem „koniec”.
"""
import asyncio
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .. import auth, live
from .deps import LIVE_PATH, client_ip, require_user

router = APIRouter()
POLL_S = 5          # jak często zaglądamy do bazy (worker zapisuje stan co ~30 s)
PING_S = 25         # komentarz podtrzymujący — NPM zamyka ciche połączenie po 60 s
MAX_S = 15 * 60     # potem przeglądarka sama łączy się od nowa (i przechodzi przez logowanie jeszcze raz)


def _peek(engine, token, ip):
    with engine.begin() as conn:
        if not auth.load_session(conn, token, ip, touch=False):
            return None
        return live.version(conn)


async def _events(request: Request, token: str, ip: str):
    engine = request.app.state.engine
    started = last_sent = time.monotonic()
    sent = None
    yield "retry: 5000\n\n"
    while True:
        if await request.is_disconnected():
            return
        v = await asyncio.to_thread(_peek, engine, token, ip)
        now = time.monotonic()
        if v is None:
            yield "event: koniec\ndata: \n\n"
            return
        if v != sent:
            yield f"event: stan\ndata: {v}\n\n"
            sent, last_sent = v, now
        elif now - last_sent >= PING_S:
            yield ": ping\n\n"
            last_sent = now
        if now - started >= MAX_S:
            return
        await asyncio.sleep(POLL_S)


@router.get(LIVE_PATH)
def stream(request: Request, sess: dict = Depends(require_user)):
    return StreamingResponse(_events(request, sess["token"], client_ip(request)), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})
