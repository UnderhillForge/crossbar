"""WebSocket session: accept, line buffer, backspace, and resume."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from starlette.websockets import WebSocket, WebSocketDisconnect

from crossbar.commands import submit
from crossbar.config import MAX_LINE
from crossbar.lobby import banner, boot_steps, login_prompt
from crossbar.session import Session, get_session, hides_input, prompt_for, valid_sid

logger = logging.getLogger("crossbar")


def hello(sess: Session) -> str:
    """Reset the edit state and greet a newly attached socket."""
    sess.line = ""
    sess.esc = None
    sess.hist_i = None
    sess.draft = ""
    sess.swallow_lf = False
    if sess.user:
        if sess.host == "grayline":
            from crossbar.lobby import header_line

            return header_line(sess.user) + prompt_for(sess)
        return f"resumed {sess.user}@{sess.host}\r\n{prompt_for(sess)}"
    return banner() + login_prompt()


def _params(text: str) -> bool:
    return all(ch.isdigit() or ch in ";?" for ch in text)


def esc_action(buf: str) -> str | None:
    """None: still collecting. '' : ignore. 'up' / 'down': history."""
    if not buf:
        return None
    kind = buf[0]
    if kind == "[":
        if len(buf) == 1:
            return None
        final = buf[-1]
        if not ("@" <= final <= "~"):
            return "" if len(buf) > 16 else None
        body = buf[1:]
        if final == "A" and (body == "A" or _params(body[:-1])):
            return "up"
        if final == "B" and (body == "B" or _params(body[:-1])):
            return "down"
        return ""
    if kind == "O":
        if len(buf) == 1:
            return None
        if buf[1] == "A":
            return "up"
        if buf[1] == "B":
            return "down"
        return ""
    return ""


def _replace_line(sess: Session, new_line: str) -> str:
    old = sess.line
    sess.line = new_line
    return ("\b \b" * len(old)) + new_line


def _recall(sess: Session, way: str) -> str:
    if way == "up":
        if not sess.history:
            return ""
        if sess.hist_i is None:
            sess.draft = sess.line
            sess.hist_i = len(sess.history) - 1
        elif sess.hist_i == 0:
            return ""
        else:
            sess.hist_i -= 1
        return _replace_line(sess, sess.history[sess.hist_i])
    if sess.hist_i is None:
        return ""
    if sess.hist_i >= len(sess.history) - 1:
        sess.hist_i = None
        return _replace_line(sess, sess.draft)
    sess.hist_i += 1
    return _replace_line(sess, sess.history[sess.hist_i])


def push(sess: Session, data: str) -> str:
    """Fold keystrokes into the line buffer. Backspace echoes \\b \\b."""
    if sess.host in {"bec", "bec-mf"} and "\x03" in data:
        from crossbar import v7

        sess.line = ""
        sess.hist_i = None
        sess.draft = ""
        extra = v7.interrupt(sess)
        return "^C\r\n" + extra + prompt_for(sess)
    if sess.v7_phase == "pico":
        from crossbar.pico import feed

        return feed(sess, data)
    out: list[str] = []
    for ch in data:
        if sess.swallow_lf:
            sess.swallow_lf = False
            if ch == "\n":
                continue
        if sess.esc is not None:
            sess.esc += ch
            action = esc_action(sess.esc)
            if action is None:
                continue
            sess.esc = None
            if action in ("up", "down") and not hides_input(sess):
                out.append(_recall(sess, action))
            continue
        if ch == "\x1b":
            sess.esc = ""
            continue
        if ch in ("\x7f", "\x08"):
            if sess.line:
                sess.line = sess.line[:-1]
                sess.hist_i = None
                if not hides_input(sess):
                    out.append("\b \b")
            continue
        if ch == "\x03":
            sess.line = ""
            sess.hist_i = None
            sess.draft = ""
            if sess.user is None:
                sess.phase = "login"
                sess.pending_handle = ""
                sess.pending_password = ""
                out.append("^C\r\n" + login_prompt())
            elif sess.host in {"bec", "bec-mf"}:
                from crossbar import v7

                extra = v7.interrupt(sess)
                out.append("^C\r\n" + extra + prompt_for(sess))
            else:
                if sess.phase.startswith("profile"):
                    sess.phase = "shell"
                    sess.pending_password = ""
                if sess.phase.startswith("mail"):
                    from crossbar.commands import _clear_mail_draft

                    _clear_mail_draft(sess)
                    sess.phase = "shell"
                out.append("^C\r\n" + prompt_for(sess))
            continue
        if (
            ch == "\t"
            and sess.user
            and sess.host == "grayline"
            and not sess.phase.startswith("profile")
            and not sess.phase.startswith("mail")
        ):
            from crossbar.lobby import pad_tab

            echo, sess.line = pad_tab(sess.line, prompt_for(sess))
            out.append(echo)
            continue
        if ch == "\r" or ch == "\n":
            if ch == "\r":
                sess.swallow_lf = True
            out.append("\r\n")
            out.append(submit(sess))
            continue
        if ch < " " or ch > "~":
            continue
        sess.hist_i = None
        if len(sess.line) >= MAX_LINE:
            out.append("\a")
            continue
        sess.line += ch
        if not hides_input(sess):
            out.append(ch)
    return "".join(out)


async def _wait_key(ws: WebSocket, delay: float) -> bool:
    try:
        message = await asyncio.wait_for(ws.receive(), timeout=delay)
    except asyncio.TimeoutError:
        return False
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect()
    return True


async def play_boot(ws: WebSocket) -> None:
    """Carrier lines, then the banner. After CONNECT, any key skips the rest."""
    armed = False
    for text, delay, _kind in boot_steps():
        await ws.send_text(text)
        if "CONNECT" in text:
            armed = True
        if delay <= 0:
            continue
        if await _wait_key(ws, delay) and armed:
            return


async def _paced_send(ws: WebSocket, text: str, baud: int) -> None:
    """Token bucket on the terminal write. TCP is left alone. T1 is not shaped."""
    if baud >= 64000:
        await ws.send_text(text)
        return
    cps = max(baud / 10.0, 1.0)
    step = max(int(cps * 0.1), 8)
    for index in range(0, len(text), step):
        await ws.send_text(text[index : index + step])
        await asyncio.sleep(min(step, len(text) - index) / cps)


async def _attach(sess: Session, ws: WebSocket) -> None:
    previous = sess.socket
    sess.socket = ws
    if previous is not None and previous is not ws:
        # One identity, one terminal. The previous tab stops.
        with contextlib.suppress(Exception):
            await previous.close(code=4401)


def _detach(sess: Session, ws: WebSocket) -> None:
    if sess.socket is ws:
        sess.socket = None


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    sid = websocket.session.get("sid")
    if not valid_sid(sid):
        await websocket.send_text("this terminal needs the session cookie. reload /.\r\n")
        await websocket.close(code=4400)
        return
    assert isinstance(sid, str)
    sess = get_session(sid)
    if websocket.client is not None:
        sess.peer_ip = websocket.client.host or ""
    await _attach(sess, websocket)
    try:
        if sess.user:
            await websocket.send_text(hello(sess))
        else:
            sess.phase = "login"
            sess.pending_handle = ""
            sess.pending_password = ""
            sess.line = ""
            await play_boot(websocket)
            if sess.socket is not websocket:
                return
            sess.phase = "login"
            await websocket.send_text(login_prompt())
        while True:
            if sess.socket is not websocket:
                await websocket.close(code=4401)
                return
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if sess.socket is not websocket:
                await websocket.close(code=4401)
                return
            if message.get("text") is not None:
                data = message["text"]
            elif message.get("bytes") is not None:
                data = message["bytes"].decode("utf-8", "ignore")
            else:
                continue
            sess.last_active = time.monotonic()
            from crossbar.session import apply_kills, publish_sessions

            apply_kills()
            text = push(sess, data)
            publish_sessions()
            delay = sess.lead_sleep
            sess.lead_sleep = 0.0
            if delay:
                await asyncio.sleep(delay)
            if text and sess.socket is websocket:
                await _paced_send(websocket, text, sess.baud_now)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("session failed")
    finally:
        _detach(sess, websocket)
