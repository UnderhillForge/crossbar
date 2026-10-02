"""In-memory sessions.

One browser cookie maps to one Session. A process restart drops every
Session; the signed cookie still arrives, finds nothing, and the terminal
returns to the login banner. Accounts live in SQLite. Idle sessions are
discarded after 6 hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Protocol

from crossbar.config import IDLE_TTL, T1_BAUD

_NAME = re.compile(r"^[a-z][a-z0-9]{0,11}$")


class Socket(Protocol):
    def send_text(self, data: str) -> Awaitable[None]: ...

    def close(self, code: int = 1000) -> Awaitable[None]: ...


@dataclass
class Session:
    sid: str
    user: str | None = None
    host: str = "grayline"
    line: str = ""
    history: list[str] = field(default_factory=list)
    # monotonic clock; idle math stays put if the wall clock steps
    last_active: float = field(default_factory=time.monotonic)
    socket: Socket | None = field(default=None, repr=False)
    esc: str | None = None
    hist_i: int | None = None
    draft: str = ""
    swallow_lf: bool = False
    # login, password, new_handle, new_password, new_confirm, new_email, shell
    phase: str = "login"
    pending_handle: str = ""
    pending_password: str = field(default="", repr=False)
    previous_host: str = ""
    room: str = ""
    seen_look: bool = False
    baud_now: int = T1_BAUD
    hops: list[str] = field(default_factory=list)
    baud_stack: list[int] = field(default_factory=list)
    lead_sleep: float = 0.0
    ansi_ok: bool = False
    v7_phase: str = ""
    v7_user: str = ""
    v7_uid: int = -1
    v7_gid: int = -1
    v7_cwd: str = "/"
    v7_pending: str = ""
    v7_epoch: float = 0.0
    v7_mesg: str = "y"
    v7_path: str = "/bin:/usr/bin"
    v7_status: int = 0
    pico_path: str = ""
    pico_lines: list[str] = field(default_factory=list)
    pico_row: int = 0
    pico_col: int = 0
    pico_top: int = 0
    pico_dirty: bool = False
    pico_ro: bool = False
    pico_ask: str = ""
    pico_search: str = ""
    ed_lines: list[str] = field(default_factory=list)
    ed_path: str = ""
    ed_dirty: bool = False
    # Guest bec edits live here and are dropped on logout. Registered users use disk.
    v7_store: dict = field(default_factory=dict)
    peer_ip: str = ""
    # Wall-clock time.time() when this handle reached the pad (for finger).
    login_at: float = 0.0
    # Pad mail compose draft (cleared on cancel / logout / send).
    mail_to: str = ""
    mail_subject: str = ""
    mail_body_lines: list[str] = field(default_factory=list)
    mail_reply_to: int | None = None
    # GROUPS reader cursor + compose draft.
    group_name: str = ""
    group_art: int = 0
    group_subject: str = ""
    group_body_lines: list[str] = field(default_factory=list)
    # MudProto TCP door (external process). Never carries a Grayline handle.
    mud_writer: object | None = field(default=None, repr=False)
    mud_task: object | None = field(default=None, repr=False)
    mud_pending_connect: bool = False
    mud_pending_hangup: bool = False
    mud_closing: bool = False


# sid -> Session. Process-local. Restart clears this dict.
SESSIONS: dict[str, Session] = {}


def valid_sid(value: object) -> bool:
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        return False
    return value.isascii() and all(c.isalnum() or c in "-_" for c in value)


def valid_name(name: str) -> bool:
    # guest, new, and login are prompt words, not account names.
    return name not in {"new", "login", "guest"} and _NAME.fullmatch(name) is not None


def hides_input(sess: Session) -> bool:
    return sess.phase in {
        "password",
        "new_password",
        "new_confirm",
        "profile_password",
        "profile_password2",
    } or sess.v7_phase == "password"


def prompt_for(sess: Session) -> str:
    if sess.host in {"bec", "bec-mf"}:
        if sess.v7_phase in {"", "login"}:
            return "login: "
        if sess.v7_phase == "password":
            return "Password:"
        # ed has no shell prompt. A bec$ here makes every ed "?" look like the shell.
        if sess.v7_phase == "ed":
            return ""
        mark = "#" if sess.v7_uid == 0 else "$"
        name = "mf" if sess.host == "bec-mf" else "bec"
        base = f"{name}{mark} "
        if sess.ansi_ok:
            return f"\x1b[32m{base}\x1b[0m"
        return base
    if sess.user is None:
        if sess.host in {"", "grayline"}:
            return "LOGON: "
        return "login: "
    if sess.host == "grayline":
        from crossbar.lobby import pad_path_for, render_pad_prompt

        base = render_pad_prompt(sess.user, pad_path_for(sess.phase))
        if sess.phase == "profile_email":
            return base + "EMAIL: "
        if sess.phase == "profile_password":
            return base + "PASSWORD: "
        if sess.phase == "profile_password2":
            return base + "CONFIRM: "
        if sess.phase == "profile_ps1":
            return base + "PS1: "
        if sess.phase == "mail_subject":
            return base + "Subject: "
        if sess.phase == "group_subject":
            return base + "Subject: "
        return base
    return f"{sess.user}@{sess.host}> "


def ensure_path(sess: Session) -> None:
    if sess.hops:
        return
    if sess.host in {"", "grayline"}:
        sess.hops = ["grayline"]
        sess.baud_stack = [T1_BAUD]
        sess.baud_now = T1_BAUD
        return
    sess.hops = [sess.host]
    sess.baud_stack = [sess.baud_now]


def push_hop(sess: Session, dest: str, ceiling: int) -> None:
    """Clamp baud downward. The saved stack is what a hangup restores."""
    ensure_path(sess)
    if dest == sess.host:
        return
    new_baud = min(sess.baud_now, ceiling)
    sess.previous_host = sess.host
    sess.hops.append(dest)
    sess.baud_stack.append(new_baud)
    sess.baud_now = new_baud
    sess.host = dest


def pop_hop(sess: Session) -> str:
    ensure_path(sess)
    if len(sess.hops) > 1:
        sess.hops.pop()
        sess.baud_stack.pop()
    sess.host = sess.hops[-1]
    sess.baud_now = sess.baud_stack[-1]
    if sess.host == "grayline" and sess.hops == ["grayline"]:
        sess.baud_now = T1_BAUD
        sess.baud_stack = [T1_BAUD]
    sess.previous_host = sess.hops[-2] if len(sess.hops) > 1 else ""
    return sess.host


def live_sessions() -> list[Session]:
    now = time.monotonic()
    return [
        sess
        for sess in SESSIONS.values()
        if sess.user and now - sess.last_active <= IDLE_TTL
    ]


def _schedule_kick(sess: Session, message: str, code: int) -> None:
    ws = sess.socket
    sess.socket = None
    sess.user = None
    if ws is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_close_ws(ws, message, code))


async def _close_ws(ws: Socket, message: str, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(message)
        await ws.close(code=code)


def get_session(sid: str) -> Session:
    now = time.monotonic()
    current = SESSIONS.get(sid)
    if current is not None and now - current.last_active > IDLE_TTL:
        SESSIONS.pop(sid, None)
        _schedule_kick(current, "\r\nidle 6h — carrier dropped\r\n", 4408)
        current = None
    if current is None:
        current = Session(sid=sid, last_active=now)
        SESSIONS[sid] = current
    else:
        current.last_active = now
    return current


def publish_sessions() -> None:
    """Write a local snapshot the operator console can read. Full IP stays in the file, not the HTML."""
    import hashlib
    import json

    from crossbar import site

    rows = []
    now = time.monotonic()
    for sess in SESSIONS.values():
        ip = sess.peer_ip or ""
        rows.append(
            {
                "sid": sess.sid,
                "handle": sess.user or "",
                "dest": sess.host,
                "baud": sess.baud_now,
                "idle": int(now - sess.last_active),
                "ip": ip,
                "ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:12] if ip else "",
            }
        )
    path = site.site_dir() / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def apply_kills() -> None:
    import json

    from crossbar import site

    path = site.site_dir() / "kills.json"
    if not path.is_file():
        return
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    path.unlink(missing_ok=True)
    if not isinstance(loaded, list):
        return
    for sid in loaded:
        sess = SESSIONS.pop(str(sid), None)
        if sess is not None:
            _schedule_kick(sess, "\r\ncarrier dropped\r\n", 4408)


async def idle_sweep() -> None:
    try:
        while True:
            await asyncio.sleep(60)
            apply_kills()
            publish_sessions()
            now = time.monotonic()
            stale = [
                sess
                for sess in list(SESSIONS.values())
                if now - sess.last_active > IDLE_TTL
            ]
            for sess in stale:
                SESSIONS.pop(sess.sid, None)
                ws = sess.socket
                sess.socket = None
                sess.user = None
                if ws is not None:
                    await _close_ws(ws, "\r\nidle 6h — carrier dropped\r\n", 4408)
    except asyncio.CancelledError:
        return
