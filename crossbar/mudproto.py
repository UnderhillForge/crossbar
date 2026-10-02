"""MudProto door — TCP pipe to an external MUD process.

MudProto owns login. Crossbar never sends a Grayline handle, !name, or
!account. One TCP connection per pad session. Lines are \\n-terminated on
the wire; the xterm sees \\r\\n.

Config:
  MUDPROTO_HOST  (default 127.0.0.1)
  MUDPROTO_PORT  (default 4000)

Hangup: socket close, a line that is exactly ``~.`` or ``QUIT``, or bye/^C
from the pad. ``/quit`` is forwarded to MudProto and is not disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crossbar.session import Session

logger = logging.getLogger("crossbar.mudproto")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4000
CONNECT_TIMEOUT = 5.0
PROBE_TIMEOUT = 2.0
BAUD = 9600


def door_host() -> str:
    return os.environ.get("MUDPROTO_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def door_port() -> int:
    raw = os.environ.get("MUDPROTO_PORT", str(DEFAULT_PORT)).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def _to_terminal(data: bytes) -> str:
    text = data.decode("utf-8", "ignore")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", "\r\n")


def health(timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Connect and expect a MUDPROTO / name: style banner. For ops/tests."""
    host, port = door_host(), door_port()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            chunks: list[bytes] = []
            while True:
                try:
                    piece = sock.recv(4096)
                except socket.timeout:
                    break
                if not piece:
                    break
                chunks.append(piece)
                blob = b"".join(chunks).decode("utf-8", "ignore").lower()
                if "name:" in blob or "mudproto" in blob:
                    return True, b"".join(chunks).decode("utf-8", "ignore")
                if len(b"".join(chunks)) > 8192:
                    break
    except OSError as exc:
        return False, str(exc)
    text = b"".join(chunks).decode("utf-8", "ignore") if chunks else ""
    if "name:" in text.lower() or "mudproto" in text.lower():
        return True, text
    return False, text or "no banner"


async def _close_writer(sess: Session) -> None:
    writer = getattr(sess, "mud_writer", None)
    sess.mud_writer = None
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        logger.debug("mudproto writer close failed", exc_info=True)


async def _cancel_pump(sess: Session) -> None:
    task = getattr(sess, "mud_task", None)
    sess.mud_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("mudproto pump cancel failed", exc_info=True)


def _return_pad_text(sess: Session) -> str:
    from crossbar.lobby import return_to_pad
    from crossbar.session import pop_hop

    if sess.host == "mudproto":
        pop_hop(sess)
    if sess.host != "grayline":
        from crossbar.config import T1_BAUD

        sess.host = "grayline"
        sess.hops = ["grayline"]
        sess.baud_stack = [T1_BAUD]
        sess.baud_now = T1_BAUD
    return return_to_pad(sess.user or "guest")


async def hangup(sess: Session, *, announce: bool = True) -> str:
    """Tear down the TCP door and return to the pad."""
    sess.mud_closing = True
    sess.mud_pending_connect = False
    sess.mud_pending_hangup = False
    await _cancel_pump(sess)
    await _close_writer(sess)
    body = ""
    if announce:
        body = "CARRIER LOST\r\n"
    body += _return_pad_text(sess)
    sess.mud_closing = False
    return body


def begin_dial(sess: Session) -> str:
    """Mark the session for async dial from the WebSocket loop."""
    from crossbar.session import push_hop

    if getattr(sess, "mud_writer", None) is not None:
        return "already connected to mudproto\r\n"
    push_hop(sess, "mudproto", BAUD)
    sess.mud_pending_connect = True
    sess.mud_closing = False
    sess.mud_pending_hangup = False
    sess.lead_sleep = 0.4
    return f"DIALING {BAUD}...\r\n"


async def attach(sess: Session, send) -> str:
    """Open TCP after DIALING. ``send`` is an async callable(str)."""
    sess.mud_pending_connect = False
    host, port = door_host(), door_port()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=CONNECT_TIMEOUT,
        )
    except Exception as exc:
        logger.info("mudproto dial failed %s:%s: %s", host, port, exc)
        from crossbar.session import pop_hop

        if sess.host == "mudproto":
            pop_hop(sess)
        return "NO CARRIER\r\n"

    sess.mud_writer = writer
    # Do not send Grayline identity — MudProto owns name/password.
    sess.mud_task = asyncio.create_task(
        _pump(sess, reader, send),
        name=f"mudproto-{sess.sid[:8]}",
    )
    return ""


async def _pump(sess: Session, reader: asyncio.StreamReader, send) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            text = _to_terminal(data)
            if text:
                await send(text)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mudproto pump failed sid=%s", sess.sid)
    finally:
        sess.mud_task = None
        writer = getattr(sess, "mud_writer", None)
        sess.mud_writer = None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        # Pad-initiated hangup owns the farewell; remote EOF announces here.
        if sess.mud_closing:
            return
        if sess.host == "mudproto":
            try:
                await send("CARRIER LOST\r\n" + _return_pad_text(sess))
            except Exception:
                _return_pad_text(sess)


def on_line(sess: Session, text: str) -> str:
    """Forward one submitted line to MudProto. Sync; schedules drain."""
    stripped = text.replace("\r", "").rstrip("\n")
    # Exact QUIT / ~. hang up. bye is the pad hangup verb. /quit stays with MudProto.
    if stripped in {"~.", "QUIT"} or stripped.lower() == "bye":
        sess.mud_pending_hangup = True
        return ""
    writer = getattr(sess, "mud_writer", None)
    if writer is None:
        sess.mud_pending_hangup = True
        return ""
    payload = (stripped + "\n").encode("utf-8", "ignore")
    try:
        writer.write(payload)
    except Exception:
        sess.mud_pending_hangup = True
        return ""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(writer.drain())
    except RuntimeError:
        pass
    return ""


def request_hangup(sess: Session) -> None:
    sess.mud_pending_hangup = True
