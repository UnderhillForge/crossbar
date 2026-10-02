"""SQLite accounts for GRAYLINE. No mail is sent from here."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# These handles cannot register or authenticate. sysop and admin have no
# working password even if a row is inserted by hand.
RESERVED = frozenset(
    {
        "sysop",
        "admin",
        "root",
        "operator",
        "postmaster",
        "grayline",
        "crossbar",
    }
)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    handle TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    email TEXT NOT NULL,
    created TEXT NOT NULL,
    last_login TEXT,
    status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS achievements (
    owner TEXT NOT NULL,
    code TEXT NOT NULL,
    earned TEXT NOT NULL,
    PRIMARY KEY (owner, code)
);
CREATE TABLE IF NOT EXISTS flag_claims (
    handle TEXT NOT NULL,
    flag_id TEXT NOT NULL,
    ps1 TEXT NOT NULL,
    txid TEXT NOT NULL,
    earned TEXT NOT NULL,
    PRIMARY KEY (handle, flag_id)
);
"""

_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def db_path() -> Path:
    override = os.environ.get("CROSSBAR_DB", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "grayline.db"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "must_change" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
    if "note" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "wallet" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN wallet TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS accounts_wallet_unique ON accounts(wallet) WHERE wallet != ''"
    )
    from crossbar import mail
    from crossbar import wall

    mail.ensure_schema(conn)
    wall.ensure_schema(conn)


def init_db() -> None:
    connect()


def connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        _conn.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    _conn = conn
    _conn_path = path
    return conn


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if kind != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, bytes.fromhex(hash_hex))


@dataclass(frozen=True)
class Account:
    handle: str
    password_hash: str
    email: str
    created: str
    last_login: str | None
    status: str


def _row(handle: str) -> Account | None:
    cur = connect().execute(
        "SELECT handle, password_hash, email, created, last_login, status "
        "FROM accounts WHERE handle = ?",
        (handle,),
    )
    found = cur.fetchone()
    if found is None:
        return None
    return Account(
        handle=found["handle"],
        password_hash=found["password_hash"],
        email=found["email"],
        created=found["created"],
        last_login=found["last_login"],
        status=found["status"],
    )


def get_account(handle: str) -> Account | None:
    return _row(handle.strip().lower())


def create_account(handle: str, password: str, email: str) -> Account:
    name = handle.strip().lower()
    if name in RESERVED:
        raise ValueError("reserved")
    if get_account(name) is not None:
        raise ValueError("taken")
    stamp = _now()
    digest = hash_password(password)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO accounts (handle, password_hash, email, created, last_login, status) "
            "VALUES (?, ?, ?, ?, ?, 'ok')",
            (name, digest, email.strip(), stamp, stamp),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("taken") from exc
    made = get_account(name)
    if made is None:
        raise ValueError("taken")
    return made


def authenticate(handle: str, password: str) -> bool:
    name = handle.strip().lower()
    # Reserved names never authenticate, including sysop and admin.
    if name in RESERVED:
        return False
    account = get_account(name)
    if account is None or account.status != "ok":
        return False
    if not verify_password(password, account.password_hash):
        return False
    connect().execute(
        "UPDATE accounts SET last_login = ? WHERE handle = ?",
        (_now(), name),
    )
    connect().commit()
    return True


def grant_achievement(owner: str, code: str) -> bool:
    """Record an achievement code for an account or a session id. True if new."""
    conn = connect()
    cur = conn.execute(
        "INSERT INTO achievements (owner, code, earned) VALUES (?, ?, ?) "
        "ON CONFLICT (owner, code) DO NOTHING",
        (owner, code, _now()),
    )
    conn.commit()
    return cur.rowcount == 1


def list_accounts() -> list[dict]:
    rows = connect().execute(
        "SELECT handle, email, created, last_login, status, must_change, note, wallet "
        "FROM accounts ORDER BY handle"
    ).fetchall()
    return [
        {
            "handle": row["handle"],
            "email": row["email"],
            "created": row["created"],
            "last_login": row["last_login"],
            "status": row["status"],
            "must_change": int(row["must_change"] or 0),
            "note": row["note"] or "",
            "wallet": row["wallet"] or "",
        }
        for row in rows
    ]


def set_account_fields(
    handle: str,
    *,
    status: str | None = None,
    note: str | None = None,
    wallet: str | None = None,
    email: str | None = None,
    password_hash: str | None = None,
    must_change: int | None = None,
) -> None:
    sets: list[str] = []
    values: list[object] = []
    if status is not None:
        sets.append("status = ?")
        values.append(status)
    if note is not None:
        sets.append("note = ?")
        values.append(note)
    if email is not None:
        sets.append("email = ?")
        values.append(email)
    if wallet is not None:
        sets.append("wallet = ?")
        values.append(wallet)
    if password_hash is not None:
        sets.append("password_hash = ?")
        values.append(password_hash)
    if must_change is not None:
        sets.append("must_change = ?")
        values.append(must_change)
    if not sets:
        return
    values.append(handle)
    conn = connect()
    conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE handle = ?", values)
    conn.commit()


def wallet_of(handle: str) -> str:
    row = connect().execute("SELECT wallet FROM accounts WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        return ""
    return str(row["wallet"] or "")


def email_of(handle: str) -> str:
    row = connect().execute("SELECT email FROM accounts WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        return ""
    return str(row["email"] or "")


def note_of(handle: str) -> str:
    row = connect().execute("SELECT note FROM accounts WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        return ""
    return str(row["note"] or "")


def wallet_owner(ps1: str) -> str | None:
    row = connect().execute(
        "SELECT handle FROM accounts WHERE wallet = ? AND wallet != ''",
        (ps1,),
    ).fetchone()
    if row is None:
        return None
    return str(row["handle"])


def claim_recorded(handle: str, flag_id: str) -> bool:
    row = connect().execute(
        "SELECT 1 FROM flag_claims WHERE handle = ? AND flag_id = ?",
        (handle, flag_id),
    ).fetchone()
    return row is not None


def claim_ranks() -> dict[str, tuple[int, int]]:
    """Competition rank and claim count for accounts that have at least one claim."""
    rows = connect().execute(
        "SELECT handle, COUNT(*) AS n FROM flag_claims GROUP BY handle"
    ).fetchall()
    ordered = sorted(
        ((str(row["handle"]), int(row["n"])) for row in rows if int(row["n"]) > 0),
        key=lambda item: (-item[1], item[0]),
    )
    ranks: dict[str, tuple[int, int]] = {}
    place = 0
    seen = 0
    previous: int | None = None
    for handle, count in ordered:
        seen += 1
        if count != previous:
            place = seen
            previous = count
        ranks[handle] = (place, count)
    return ranks


def record_claim(handle: str, flag_id: str, ps1: str, txid: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO flag_claims (handle, flag_id, ps1, txid, earned) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (handle, flag_id) DO UPDATE SET ps1 = excluded.ps1, txid = excluded.txid, earned = excluded.earned",
        (handle, flag_id, ps1, txid, _now()),
    )
    conn.commit()


def revoke_achievement(owner: str, code: str) -> None:
    conn = connect()
    conn.execute("DELETE FROM achievements WHERE owner = ? AND code = ?", (owner, code))
    conn.commit()


def achievements_for(owner: str) -> list[str]:
    rows = connect().execute(
        "SELECT code FROM achievements WHERE owner = ? ORDER BY code",
        (owner,),
    ).fetchall()
    return [row["code"] for row in rows]
