"""Pad-local Grayline discussion groups (Usenet-style).

NEWS remains the system bulletin (news.asc). Packet mesh and NNTP are later.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

from crossbar.accounts import _now, connect, get_account
from crossbar.config import SYSTEM_NAME

SUBJECT_MAX = 60
BODY_MAX_CHARS = 4000
BODY_MAX_LINES = 60
HEADERS_DEFAULT = 20
HEADERS_MAX = 50

_GROUP_NAME = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    post_policy TEXT NOT NULL DEFAULT 'members',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    group_name TEXT NOT NULL,
    number INTEGER NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    from_handle TEXT NOT NULL,
    date_sent TEXT NOT NULL,
    date_arrived TEXT NOT NULL,
    references_hdr TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local',
    UNIQUE (group_name, number)
);

CREATE INDEX IF NOT EXISTS articles_group_number
    ON articles(group_name, number);
CREATE INDEX IF NOT EXISTS articles_message_id
    ON articles(message_id);

CREATE TABLE IF NOT EXISTS group_read (
    handle TEXT NOT NULL,
    group_name TEXT NOT NULL,
    last_read INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (handle, group_name)
);
"""

_SEEDS = (
    ("grayline.general", "General hangout", "members"),
    ("grayline.doors", "Circuits and doors", "members"),
    ("grayline.sysop", "Sysop notices (read mostly)", "sysop"),
)


@dataclass(frozen=True)
class GroupInfo:
    name: str
    description: str
    post_policy: str
    high: int
    unread: int


@dataclass(frozen=True)
class ArticleHeader:
    number: int
    subject: str
    from_handle: str
    date_sent: str
    message_id: str


@dataclass(frozen=True)
class Article:
    number: int
    subject: str
    from_handle: str
    date_sent: str
    date_arrived: str
    message_id: str
    references: str
    path: str
    body: str
    source: str
    group_name: str


def site_tag() -> str:
    tag = os.environ.get("CROSSBAR_SITE_ID", "").strip().lower()
    return tag or SYSTEM_NAME


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    seed_default_groups(conn)


def seed_default_groups(conn: sqlite3.Connection | None = None) -> None:
    db = conn or connect()
    now = _now()
    for name, description, policy in _SEEDS:
        db.execute(
            "INSERT OR IGNORE INTO groups (name, description, post_policy, created) "
            "VALUES (?, ?, ?, ?)",
            (name, description, policy, now),
        )
    if conn is None:
        db.commit()


def _normalize_body(body: str) -> str:
    text = body.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text.strip("\n")


def _validate_payload(subject: str, body: str) -> tuple[str, str]:
    subj = subject.replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    if len(subj) > SUBJECT_MAX:
        raise ValueError("subject too long")
    text = _normalize_body(body)
    if not text.strip():
        raise ValueError("empty body")
    lines = text.split("\n")
    if len(lines) > BODY_MAX_LINES or len(text) > BODY_MAX_CHARS:
        raise ValueError("body too long")
    return subj, text


def normalize_group_name(name: str) -> str:
    return (name or "").strip().lower()


def valid_group_name(name: str) -> bool:
    return _GROUP_NAME.fullmatch(normalize_group_name(name)) is not None


def get_group(name: str) -> GroupInfo | None:
    key = normalize_group_name(name)
    row = connect().execute(
        "SELECT name, description, post_policy FROM groups WHERE name = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    high = _high_water(key)
    return GroupInfo(
        name=str(row["name"]),
        description=str(row["description"] or ""),
        post_policy=str(row["post_policy"] or "members"),
        high=high,
        unread=0,
    )


def _high_water(group: str) -> int:
    row = connect().execute(
        "SELECT MAX(number) AS n FROM articles WHERE group_name = ?",
        (group,),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def last_read(handle: str, group: str) -> int:
    name = (handle or "").strip().lower()
    if not name or name == "guest":
        return 0
    row = connect().execute(
        "SELECT last_read FROM group_read WHERE handle = ? AND group_name = ?",
        (name, normalize_group_name(group)),
    ).fetchone()
    return int(row["last_read"]) if row else 0


def unread_count(handle: str, group: str) -> int:
    name = (handle or "").strip().lower()
    if not name or name == "guest":
        return 0
    key = normalize_group_name(group)
    high = _high_water(key)
    seen = last_read(name, key)
    return max(0, high - seen)


def mark_read(handle: str, group: str, number: int) -> None:
    name = (handle or "").strip().lower()
    if not name or name == "guest":
        return
    key = normalize_group_name(group)
    num = max(0, int(number))
    conn = connect()
    current = last_read(name, key)
    if num <= current:
        return
    conn.execute(
        "INSERT INTO group_read (handle, group_name, last_read) VALUES (?, ?, ?) "
        "ON CONFLICT(handle, group_name) DO UPDATE SET last_read = excluded.last_read "
        "WHERE excluded.last_read > group_read.last_read",
        (name, key, num),
    )
    conn.commit()


def list_groups(*, for_handle: str = "") -> list[GroupInfo]:
    rows = connect().execute(
        "SELECT name, description, post_policy FROM groups ORDER BY name"
    ).fetchall()
    who = (for_handle or "").strip().lower()
    out: list[GroupInfo] = []
    for row in rows:
        name = str(row["name"])
        high = _high_water(name)
        unread = unread_count(who, name) if who and who != "guest" else 0
        out.append(
            GroupInfo(
                name=name,
                description=str(row["description"] or ""),
                post_policy=str(row["post_policy"] or "members"),
                high=high,
                unread=unread,
            )
        )
    return out


def _header_from_row(row: sqlite3.Row) -> ArticleHeader:
    return ArticleHeader(
        number=int(row["number"]),
        subject=str(row["subject"] or ""),
        from_handle=str(row["from_handle"]),
        date_sent=str(row["date_sent"]),
        message_id=str(row["message_id"]),
    )


def _article_from_row(row: sqlite3.Row) -> Article:
    return Article(
        number=int(row["number"]),
        subject=str(row["subject"] or ""),
        from_handle=str(row["from_handle"]),
        date_sent=str(row["date_sent"]),
        date_arrived=str(row["date_arrived"]),
        message_id=str(row["message_id"]),
        references=str(row["references_hdr"] or ""),
        path=str(row["path"] or ""),
        body=str(row["body"] or ""),
        source=str(row["source"] or "local"),
        group_name=str(row["group_name"]),
    )


def headers(group: str, *, limit: int = HEADERS_DEFAULT) -> list[ArticleHeader]:
    key = normalize_group_name(group)
    if get_group(key) is None:
        raise ValueError("no such group")
    cap = max(1, min(int(limit), HEADERS_MAX))
    rows = connect().execute(
        "SELECT number, subject, from_handle, date_sent, message_id "
        "FROM articles WHERE group_name = ? "
        "ORDER BY number DESC LIMIT ?",
        (key, cap),
    ).fetchall()
    return [_header_from_row(row) for row in rows]


def unread_headers(
    handle: str,
    *,
    limit: int = 50,
    group: str | None = None,
) -> list[tuple[str, ArticleHeader]]:
    """Unread article headers oldest-first. Optional single-group filter."""
    name = (handle or "").strip().lower()
    if not name or name == "guest":
        return []
    cap = max(1, min(int(limit), 100))
    if group:
        keys = [normalize_group_name(group)]
        if get_group(keys[0]) is None:
            raise ValueError("no such group")
    else:
        keys = [info.name for info in list_groups()]
    out: list[tuple[str, ArticleHeader]] = []
    for key in keys:
        seen = last_read(name, key)
        rows = connect().execute(
            "SELECT number, subject, from_handle, date_sent, message_id "
            "FROM articles WHERE group_name = ? AND number > ? "
            "ORDER BY number ASC",
            (key, seen),
        ).fetchall()
        for row in rows:
            out.append((key, _header_from_row(row)))
            if len(out) >= cap:
                return out
    return out


def get_article(group: str, number: int) -> Article | None:
    key = normalize_group_name(group)
    row = connect().execute(
        "SELECT * FROM articles WHERE group_name = ? AND number = ?",
        (key, int(number)),
    ).fetchone()
    return _article_from_row(row) if row else None


def next_article(group: str, number: int) -> Article | None:
    key = normalize_group_name(group)
    row = connect().execute(
        "SELECT * FROM articles WHERE group_name = ? AND number > ? "
        "ORDER BY number ASC LIMIT 1",
        (key, int(number)),
    ).fetchone()
    return _article_from_row(row) if row else None


def prev_article(group: str, number: int) -> Article | None:
    key = normalize_group_name(group)
    row = connect().execute(
        "SELECT * FROM articles WHERE group_name = ? AND number < ? "
        "ORDER BY number DESC LIMIT 1",
        (key, int(number)),
    ).fetchone()
    return _article_from_row(row) if row else None


def _can_post(policy: str, handle: str) -> str | None:
    """Return an error string, or None if allowed."""
    name = (handle or "").strip().lower()
    if not name or name == "guest":
        return "logon required"
    if policy == "readonly":
        return "read only"
    if policy == "sysop":
        return "sysop only"
    if policy != "members":
        return "read only"
    account = get_account(name)
    if account is None or account.status != "ok":
        return "logon required"
    return None


def post(
    *,
    group: str,
    from_handle: str,
    subject: str,
    body: str,
    references: str = "",
    source: str = "local",
    force_sysop: bool = False,
) -> int:
    """Post an article. Returns the new per-group article number."""
    key = normalize_group_name(group)
    info = get_group(key)
    if info is None:
        raise ValueError("no such group")
    who = (from_handle or "").strip().lower()
    if force_sysop and who == "sysop":
        pass
    else:
        err = _can_post(info.post_policy, who)
        if err:
            raise ValueError(err)
    subj, text = _validate_payload(subject, body)
    refs = (references or "").replace("\x00", "").strip()
    now = _now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        high = conn.execute(
            "SELECT MAX(number) AS n FROM articles WHERE group_name = ?",
            (key,),
        ).fetchone()
        number = int(high["n"] or 0) + 1
        # Temporary Message-ID; rewrite with row id after insert.
        temp_mid = f"<pending.{now}.{number}@{site_tag()}>"
        cur = conn.execute(
            "INSERT INTO articles ("
            "message_id, group_name, number, subject, from_handle, "
            "date_sent, date_arrived, references_hdr, path, body, source"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
            (temp_mid, key, number, subj, who, now, now, refs, text, source),
        )
        row_id = int(cur.lastrowid)
        compact = now.replace("-", "").replace(":", "").replace("T", "").rstrip("Z")
        message_id = f"<{row_id}.{compact}@{site_tag()}>"
        conn.execute(
            "UPDATE articles SET message_id = ? WHERE id = ?",
            (message_id, row_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return number
