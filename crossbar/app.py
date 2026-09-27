"""HTTP routes, the signed session cookie, and the localhost server entry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute

from crossbar.accounts import init_db
from crossbar.config import COOKIE_MAX_AGE, COOKIE_NAME, HOST, PORT, SECRET, USING_DEV_SECRET
from crossbar.session import idle_sweep, valid_sid
from crossbar.ws import websocket_endpoint

logger = logging.getLogger("crossbar")
_INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


async def homepage(request: Request) -> Response:
    sid = request.session.get("sid")
    if not valid_sid(sid):
        request.session["sid"] = secrets.token_hex(16)
    if not _INDEX.is_file():
        return PlainTextResponse("static/index.html is missing\n", status_code=500)
    return HTMLResponse(
        _INDEX.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


async def favicon(request: Request) -> Response:
    return Response(status_code=204)


async def not_found(request: Request, exc: Exception) -> Response:
    return PlainTextResponse("not found\n", status_code=404)


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    if USING_DEV_SECRET:
        logger.warning("CROSSBAR_SECRET is unset; using the local dev fallback")
    init_db()
    task = asyncio.create_task(idle_sweep())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def build_app() -> Starlette:
    application = Starlette(
        routes=[
            Route("/", homepage, methods=["GET"]),
            Route("/favicon.ico", favicon, methods=["GET"]),
            WebSocketRoute("/ws", websocket_endpoint),
        ],
        lifespan=lifespan,
        exception_handlers={404: not_found},
    )
    application.add_middleware(
        SessionMiddleware,
        secret_key=SECRET,
        session_cookie=COOKIE_NAME,
        max_age=COOKIE_MAX_AGE,
        same_site="lax",
        https_only=False,
    )
    return application


app = build_app()


def main() -> None:
    if sys.version_info < (3, 11):
        sys.stderr.write("Crossbar requires Python 3.11 or newer.\n")
        raise SystemExit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import uvicorn

    # Localhost only. nginx on the VPS is the public listener.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
