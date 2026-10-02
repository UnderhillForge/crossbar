"""Pad-local Grayline mail. No SMTP. Account contact email is separate."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from crossbar.accounts import RESERVED, _now, connect, get_account

SYSTEM_SENDER = "sysop"
SUBJECT_MAX = 60
BODY_MAX_CHARS = 4000
BODY_MAX_LINES = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'user',
    in_reply_to INTEGER,
    recipients TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mail_copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT 'inbox',
    read_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (message_id, owner)
);

CREATE INDEX IF NOT EXISTS mail_copies_owner_folder
    ON mail_copies(owner, folder, deleted);
CREATE INDEX IF NOT EXISTS mail_copies_owner_unread
    ON mail_copies(owner, folder, deleted, read_at);
CREATE INDEX IF NOT EXISTS mail_messages_created
    ON mail_messages(created);
"""


class NoRecipientsError(ValueError):
    """Surviving broadcast audience was empty; nothing committed."""

    def __init__(self, skipped: int = 0) -> None:
        super().__init__("no recipients")
        self.skipped = skipped


@dataclass(frozen=True)
class Letter:
    id: int
    sender: str
    subject: str
    body: str
    created: str
    kind: str
    in_reply_to: int | None
    folder: str
    read_at: str | None
    to_list: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


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
    if len(lines) > BODY_MAX_LINES:
        raise ValueError("body too long")
    if len(text) > BODY_MAX_CHARS:
        raise ValueError("body too long")
    return subj, text


def _letter_from_row(row: sqlite3.Row) -> Letter:
    reply = row["in_reply_to"]
    return Letter(
        id=int(row["id"]),
        sender=str(row["sender"]),
        subject=str(row["subject"] or ""),
        body=str(row["body"] or ""),
        created=str(row["created"]),
        kind=str(row["kind"]),
        in_reply_to=int(reply) if reply is not None else None,
        folder=str(row["folder"]),
        read_at=str(row["read_at"]) if row["read_at"] is not None else None,
        to_list=str(row["recipients"] or ""),
    )


def unread_count(owner: str) -> int:
    name = (owner or "").strip().lower()
    if not name or name == "guest":
        return 0
    row = connect().execute(
        "SELECT COUNT(*) AS n FROM mail_copies "
        "WHERE owner = ? AND deleted = 0 AND folder = 'inbox' AND read_at IS NULL",
        (name,),
    ).fetchone()
    return int(row["n"] if row else 0)


def unread_notice(owner: str) -> str:
    count = unread_count(owner)
    if count <= 0:
        return ""
    if count == 1:
        return "You have 1 new letter.\r\n"
    return f"You have {count} new letters.\r\n"


def list_letters(owner: str, folder: str = "inbox", *, limit: int = 50) -> list[Letter]:
    name = (owner or "").strip().lower()
    if not name or name == "guest":
        return []
    folder = folder.strip().lower() or "inbox"
    cap = max(1, min(int(limit), 200))
    rows = connect().execute(
        "SELECT m.id, m.sender, m.subject, m.body, m.created, m.kind, m.in_reply_to, "
        "m.recipients, c.folder, c.read_at "
        "FROM mail_copies c "
        "JOIN mail_messages m ON m.id = c.message_id "
        "WHERE c.owner = ? AND c.deleted = 0 AND c.folder = ? "
        "ORDER BY m.created DESC, m.id DESC "
        "LIMIT ?",
        (name, folder, cap),
    ).fetchall()
    return [_letter_from_row(row) for row in rows]


def get_letter(owner: str, letter_id: int) -> Letter | None:
    name = (owner or "").strip().lower()
    if not name or name == "guest":
        return None
    row = connect().execute(
        "SELECT m.id, m.sender, m.subject, m.body, m.created, m.kind, m.in_reply_to, "
        "m.recipients, c.folder, c.read_at "
        "FROM mail_copies c "
        "JOIN mail_messages m ON m.id = c.message_id "
        "WHERE c.owner = ? AND c.deleted = 0 AND m.id = ?",
        (name, int(letter_id)),
    ).fetchone()
    if row is None:
        return None
    return _letter_from_row(row)


def mark_read(owner: str, letter_id: int) -> None:
    name = (owner or "").strip().lower()
    if not name or name == "guest":
        return
    connect().execute(
        "UPDATE mail_copies SET read_at = COALESCE(read_at, ?) "
        "WHERE owner = ? AND message_id = ? AND deleted = 0",
        (_now(), name, int(letter_id)),
    )
    connect().commit()


def soft_delete(owner: str, letter_id: int) -> bool:
    name = (owner or "").strip().lower()
    if not name or name == "guest":
        return False
    cur = connect().execute(
        "UPDATE mail_copies SET deleted = 1 "
        "WHERE owner = ? AND message_id = ? AND deleted = 0",
        (name, int(letter_id)),
    )
    connect().commit()
    return cur.rowcount > 0


def archive(owner: str, letter_id: int) -> bool:
    name = (owner or "").strip().lower()
    if not name or name == "guest":
        return False
    cur = connect().execute(
        "UPDATE mail_copies SET folder = 'archive' "
        "WHERE owner = ? AND message_id = ? AND deleted = 0 "
        "AND folder IN ('inbox', 'sent')",
        (name, int(letter_id)),
    )
    connect().commit()
    return cur.rowcount > 0


def _assert_recipient(sender: str, recipient: str) -> str:
    who = recipient.strip().lower()
    if not who:
        raise ValueError("no such user")
    if who == sender.strip().lower():
        raise ValueError("cannot send to yourself")
    if who in RESERVED:
        raise ValueError(f"{who} is not accepting mail")
    account = get_account(who)
    if account is None or account.status != "ok":
        raise ValueError("no such user")
    return who


def send(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: int | None = None,
) -> int:
    from_handle = sender.strip().lower()
    if not from_handle or from_handle == "guest" or get_account(from_handle) is None:
        raise ValueError("logon required")
    recipient = _assert_recipient(from_handle, to)
    subj, text = _validate_payload(subject, body)
    conn = connect()
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO mail_messages "
            "(sender, subject, body, created, kind, in_reply_to, recipients) "
            "VALUES (?, ?, ?, ?, 'user', ?, ?)",
            (from_handle, subj, text, _now(), in_reply_to, recipient),
        )
        message_id = int(cur.lastrowid)
        stamp = _now()
        conn.execute(
            "INSERT INTO mail_copies (message_id, owner, folder, read_at, deleted) "
            "VALUES (?, ?, 'inbox', NULL, 0)",
            (message_id, recipient),
        )
        conn.execute(
            "INSERT INTO mail_copies (message_id, owner, folder, read_at, deleted) "
            "VALUES (?, ?, 'sent', ?, 0)",
            (message_id, from_handle, stamp),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return message_id


def _broadcast_label(handles: list[str] | None, survivors: list[str]) -> str:
    if handles is None:
        return "all"
    if len(survivors) <= 3:
        return ", ".join(survivors)
    return "many"


def broadcast(
    *,
    subject: str,
    body: str,
    handles: list[str] | None = None,
) -> tuple[int, int]:
    subj, text = _validate_payload(subject, body)
    conn = connect()
    if handles is None:
        rows = conn.execute(
            "SELECT handle FROM accounts WHERE status = 'ok' ORDER BY handle"
        ).fetchall()
        survivors = [str(row["handle"]) for row in rows]
        skipped = 0
    else:
        seen: set[str] = set()
        survivors = []
        skipped = 0
        for raw in handles:
            name = str(raw or "").strip().lower()
            if not name or name in seen:
                if name in seen:
                    continue
                skipped += 1
                continue
            seen.add(name)
            if name in RESERVED:
                skipped += 1
                continue
            account = get_account(name)
            if account is None or account.status != "ok":
                skipped += 1
                continue
            survivors.append(name)
    if not survivors:
        raise NoRecipientsError(skipped=skipped)
    label = _broadcast_label(handles, survivors)
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO mail_messages "
            "(sender, subject, body, created, kind, in_reply_to, recipients) "
            "VALUES (?, ?, ?, ?, 'broadcast', NULL, ?)",
            (SYSTEM_SENDER, subj, text, _now(), label),
        )
        message_id = int(cur.lastrowid)
        for owner in survivors:
            conn.execute(
                "INSERT INTO mail_copies (message_id, owner, folder, read_at, deleted) "
                "VALUES (?, ?, 'inbox', NULL, 0)",
                (message_id, owner),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(survivors), skipped
