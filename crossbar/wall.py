"""Grayline wall — last N one-line posts from registered handles."""

from __future__ import annotations

import sqlite3

from crossbar.accounts import _now, connect, get_account

WALL_LIMIT = 10
WALL_LINE_MAX = 72

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wall_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL,
    body TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wall_posts_created ON wall_posts(created DESC, id DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def list_posts(*, limit: int = WALL_LIMIT) -> list[tuple[str, str, str]]:
    """Newest first: (handle, body, created)."""
    cap = max(1, min(int(limit), WALL_LIMIT))
    rows = connect().execute(
        "SELECT handle, body, created FROM wall_posts "
        "ORDER BY created DESC, id DESC LIMIT ?",
        (cap,),
    ).fetchall()
    return [(str(r["handle"]), str(r["body"]), str(r["created"])) for r in rows]


def post(handle: str, body: str) -> None:
    name = handle.strip().lower()
    if not name or name == "guest" or get_account(name) is None:
        raise ValueError("logon required")
    text = body.replace("\r", " ").replace("\n", " ").replace("\x00", "").strip()
    if not text:
        raise ValueError("empty wall line")
    if len(text) > WALL_LINE_MAX:
        raise ValueError("line too long")
    conn = connect()
    conn.execute(
        "INSERT INTO wall_posts (handle, body, created) VALUES (?, ?, ?)",
        (name, text, _now()),
    )
    conn.commit()
