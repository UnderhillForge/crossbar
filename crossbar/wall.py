"""Grayline wall — last N one-line posts in data/text/wall.asc.

Operators may edit or trim the file over SSH. Format per post line:

    <UTC ISO8601> <handle> <text>

Blank lines and lines starting with # are ignored. Unparseable lines still
show on WALL as written. CROSSBAR_WALL overrides the path (tests).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from crossbar.accounts import _now, get_account

WALL_LIMIT = 10
WALL_LINE_MAX = 72
# Soft cap so WALL <text> cannot grow the spool without bound.
WALL_FILE_CAP = 100

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+(\S+)\s+(.*\S)\s*$"
)


def wall_path() -> Path:
    override = os.environ.get("CROSSBAR_WALL")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "text" / "wall.asc"


def ensure_schema(_conn: object = None) -> None:
    """No-op; wall lives in wall.asc. Kept so older call sites stay quiet."""
    return


def _read_raw_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _parse_line(raw: str) -> tuple[str, str, str] | None:
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    match = _LINE_RE.match(text)
    if match is None:
        # SSH freeform: show the line as written.
        return ("", text, "")
    created, handle, body = match.group(1), match.group(2), match.group(3)
    if not created.endswith("Z"):
        created = created + "Z"
    return (handle.lower(), body, created)


def list_posts(*, limit: int = WALL_LIMIT) -> list[tuple[str, str, str]]:
    """Newest first: (handle, body, created). Freeform lines use handle=''."""
    cap = max(1, min(int(limit), WALL_LIMIT))
    posts: list[tuple[str, str, str]] = []
    for raw in _read_raw_lines(wall_path()):
        parsed = _parse_line(raw)
        if parsed is not None:
            posts.append(parsed)
    posts.reverse()
    return posts[:cap]


def _format_line(handle: str, body: str, created: str) -> str:
    stamp = created if created.endswith("Z") else created + "Z"
    return f"{stamp} {handle} {body}"


def post(handle: str, body: str) -> None:
    name = handle.strip().lower()
    if not name or name == "guest" or get_account(name) is None:
        raise ValueError("logon required")
    text = body.replace("\r", " ").replace("\n", " ").replace("\x00", "").strip()
    if not text:
        raise ValueError("empty wall line")
    if len(text) > WALL_LINE_MAX:
        raise ValueError("line too long")
    path = wall_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_line = _format_line(name, text, _now())

    existing = _read_raw_lines(path)
    header = [line for line in existing if line.strip().startswith("#")]
    posts = [line for line in existing if _parse_line(line) is not None]
    posts.append(new_line)
    if len(posts) > WALL_FILE_CAP:
        posts = posts[-WALL_FILE_CAP:]

    out_lines: list[str] = []
    if header:
        out_lines.extend(header)
        if header[-1].strip():
            out_lines.append("")
    elif not path.is_file():
        out_lines.extend(
            [
                "# Grayline WALL — one post per line.",
                "# Format: <UTC ISO8601> <handle> <text>",
                "# WALL shows the last 10. Trim or edit this file over SSH.",
                "",
            ]
        )
    out_lines.extend(posts)
    payload = "\n".join(out_lines) + "\n"

    fd, tmp = tempfile.mkstemp(
        prefix="wall-",
        suffix=".asc.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle_out:
            handle_out.write(payload)
            handle_out.flush()
            os.fsync(handle_out.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
