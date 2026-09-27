"""Operator accounts, sessions, and the append-only audit log.

Separate from Greyline handles. There is no default password.
Passwords use the same stdlib scrypt KDF as player accounts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crossbar.accounts import hash_password, verify_password

_IDLE = timedelta(minutes=15)
_TOKEN_TTL = timedelta(minutes=60)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    name TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS op_sessions (
    token_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created TEXT NOT NULL,
    seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_tokens (
    token_hash TEXT PRIMARY KEY,
    expires TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    override = os.environ.get("CROSSBAR_ADMIN_DB", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "admin.db"


def audit_path() -> Path:
    override = os.environ.get("CROSSBAR_ADMIN_AUDIT", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "admin-audit.log"


_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn
    _conn_path = path
    return conn


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def operator_count() -> int:
    row = connect().execute("SELECT COUNT(*) AS n FROM operators").fetchone()
    return int(row["n"])


def issue_setup_token() -> str | None:
    """One-time bootstrap secret. None once any operator exists."""
    if operator_count():
        return None
    token = secrets.token_urlsafe(24)
    expires = _stamp(_now() + _TOKEN_TTL)
    conn = connect()
    conn.execute("DELETE FROM setup_tokens")
    conn.execute(
        "INSERT INTO setup_tokens (token_hash, expires) VALUES (?, ?)",
        (_token_hash(token), expires),
    )
    conn.commit()
    return token


def setup_open() -> bool:
    if operator_count():
        return False
    row = connect().execute(
        "SELECT expires FROM setup_tokens ORDER BY expires DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    return row["expires"] >= _stamp()


def create_operator(name: str, password: str, role: str, token: str | None = None) -> None:
    handle = name.strip().lower()
    if not handle or handle in {"admin", "sysop", "root"}:
        raise ValueError("refused name")
    if len(password) < 10:
        raise ValueError("password too short")
    if role not in {"operator", "watch"}:
        raise ValueError("role")
    conn = connect()
    if operator_count() == 0:
        if not token:
            raise ValueError("token")
        row = conn.execute(
            "SELECT expires FROM setup_tokens WHERE token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None or row["expires"] < _stamp():
            raise ValueError("token")
        conn.execute("DELETE FROM setup_tokens")
    conn.execute(
        "INSERT INTO operators (name, password_hash, role, created) VALUES (?, ?, ?, ?)",
        (handle, hash_password(password), role, _stamp()),
    )
    conn.commit()


def authenticate(name: str, password: str) -> str | None:
    handle = name.strip().lower()
    row = connect().execute(
        "SELECT password_hash, role FROM operators WHERE name = ?",
        (handle,),
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return str(row["role"])


def open_session(name: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(16)
    now = _stamp()
    conn = connect()
    conn.execute(
        "INSERT INTO op_sessions (token_hash, name, csrf, created, seen) VALUES (?, ?, ?, ?, ?)",
        (_token_hash(token), name.strip().lower(), csrf, now, now),
    )
    conn.commit()
    return token, csrf


def current_session(token: str) -> tuple[str, str, str] | None:
    """(name, role, csrf) or None if missing or idle."""
    if not token:
        return None
    conn = connect()
    row = conn.execute(
        "SELECT name, csrf, seen FROM op_sessions WHERE token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    try:
        seen = datetime.strptime(row["seen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if _now() - seen > _IDLE:
        conn.execute("DELETE FROM op_sessions WHERE token_hash = ?", (_token_hash(token),))
        conn.commit()
        return None
    now = _stamp()
    conn.execute(
        "UPDATE op_sessions SET seen = ? WHERE token_hash = ?",
        (now, _token_hash(token)),
    )
    conn.commit()
    role_row = conn.execute(
        "SELECT role FROM operators WHERE name = ?",
        (row["name"],),
    ).fetchone()
    if role_row is None:
        return None
    return str(row["name"]), str(role_row["role"]), str(row["csrf"])


def drop_session(token: str) -> None:
    if not token:
        return
    conn = connect()
    conn.execute("DELETE FROM op_sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()


def audit(operator: str, action: str, detail: str, ip: str) -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "ts": _stamp(),
            "operator": operator,
            "action": action,
            "detail": detail,
            "ip": ip,
        },
        ensure_ascii=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_audit() -> list[dict]:
    path = audit_path()
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    rows.reverse()
    return rows


def csrf_ok(expected: str, got: str) -> bool:
    if not expected or not got:
        return False
    return hmac.compare_digest(expected, got)
