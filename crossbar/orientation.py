"""Floor 0, in-process. The lab next door is a separate compose file."""

from __future__ import annotations

import hmac
from pathlib import Path

import yaml

from crossbar.accounts import achievements_for, get_account, grant_achievement
from crossbar.packs import get_pack, ta_menu
from crossbar.session import Session

_ROOT = Path(__file__).resolve().parent.parent / "packs" / "orientation"
_COPY = _ROOT / "copy"
_PACK = _ROOT / "pack.yaml"

ACHIEVEMENTS = {
    "ORIENT-1": ("found the orientation pamphlet", 3),
    "ORIENT-2": ("superintendent's notes", 7),
}
_FLAG_KEYS = {"ORIENT_1": "ORIENT-1", "ORIENT_2": "ORIENT-2"}
_NOT_A_SHELL = frozenset({"ls", "dir", "sh", "bash", "cat", "pwd"})


def door_proxy() -> None:
    """Later, a loopback proxy may attach the lab. This slice does not dial it."""
    return None


def _term(text: str) -> str:
    body = text.replace("\r\n", "\n").strip("\n")
    return body.replace("\n", "\r\n") + "\r\n"


def _fill(text: str, sess: Session | None) -> str:
    handle = sess.user if sess and sess.user else "someone"
    return text.replace("{handle}", handle).replace("{crawler}", f"Crawler {handle}")


def _read(name: str, sess: Session | None = None) -> str:
    return _fill((_COPY / name).read_text(encoding="utf-8"), sess)


def _pack() -> dict:
    return yaml.safe_load(_PACK.read_text(encoding="utf-8"))


def _flags() -> dict[str, str]:
    path = _ROOT / "flags.env"
    if not path.is_file():
        path = _ROOT / "flags.env.example"
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip()
    return found


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _owner(sess: Session) -> str:
    handle = sess.user or ""
    if handle and handle != "guest" and get_account(handle) is not None:
        return f"acct:{handle}"
    return f"sid:{sess.sid}"


def _earned(sess: Session) -> list[str]:
    return [code for code in achievements_for(_owner(sess)) if code in ACHIEVEMENTS]


def doors_text() -> str:
    return _term(_read("doors.txt"))


def enter(sess: Session) -> str:
    # Leave previous_host alone so Goodbye on Terminal Addiction still
    # returns to whoever opened that board.
    sess.host = "orientation"
    data = _pack()
    sess.room = str(data.get("start") or "stairwell")
    sess.seen_look = False
    stair = _room_copy(sess.room, sess)
    return _term(_read("pa-enter.txt", sess) + "\n\n" + stair)


def leave(sess: Session) -> str:
    prev = sess.previous_host
    sess.room = ""
    sess.seen_look = False
    sess.host = "terminal-addiction"
    if prev:
        sess.hops = [prev, "terminal-addiction"]
        sess.baud_stack = [sess.baud_now, sess.baud_now]
    else:
        sess.hops = ["terminal-addiction"]
        sess.baud_stack = [sess.baud_now]
    return ta_menu()


def _rooms() -> dict:
    return _pack()["rooms"]


def _aliases() -> dict[str, str]:
    return {str(k): str(v) for k, v in (_pack().get("aliases") or {}).items()}


def _locked() -> set[str]:
    return {str(name) for name in (_pack().get("locked") or [])}


def _canon(name: str) -> str:
    name = name.strip().lower()
    return _aliases().get(name, name)


def _room_copy(room: str, sess: Session) -> str:
    meta = _rooms()[room]
    body = _read(str(meta["copy"]), sess)
    exits = ", ".join(meta.get("exits") or [])
    return f"{meta['title']}\n\n{body}\n\nexits: {exits}"


def _go(sess: Session, args: list[str]) -> str:
    if not args:
        return "go where? the exits are listed under look.\r\n"
    dest = _canon(args[0])
    rooms = _rooms()
    if dest not in rooms:
        return f"{args[0]}: no such room\r\n"
    current = rooms.get(sess.room) or rooms["stairwell"]
    if dest not in set(current.get("exits") or []):
        return "not from here.\r\n"
    if dest in _locked():
        return _term(_read("room-superintendent-office.txt", sess))
    sess.room = dest
    return _term(_room_copy(dest, sess))


def _look(sess: Session) -> str:
    if not sess.room:
        sess.room = str(_pack().get("start") or "stairwell")
    parts = []
    if not sess.seen_look:
        sess.seen_look = True
        parts.append(_read("pa-look.txt", sess))
    parts.append(_room_copy(sess.room, sess))
    return _term("\n\n".join(parts))


def _talk(sess: Session, args: list[str]) -> str:
    if args and _canon(args[0]) == "superintendent-office":
        return _term(_read("talk-superintendent-office.txt", sess))
    room = sess.room or "stairwell"
    name = f"talk-{room}.txt"
    if not (_COPY / name).is_file():
        name = "talk-stairwell.txt"
    return _term(_read(name, sess))


def _submit(sess: Session, args: list[str]) -> str:
    if not args:
        return "usage: submit <token>\r\n"
    token = args[0].strip()
    flags = _flags()
    code = None
    for key, achievement in _FLAG_KEYS.items():
        if key in flags and _same(token, flags[key]):
            code = achievement
            break
    if code is None:
        return _term(_read("pa-wrong.txt", sess))
    title, points = ACHIEVEMENTS[code]
    fresh = grant_achievement(_owner(sess), code)
    if not fresh:
        return (
            f"{code} is already on your card.\r\n"
            "the jacket still does not fit.\r\n"
        )
    return (
        f"{code}  {title}\r\n"
        f"{points} points. the booth writes it down and looks unimpressed.\r\n"
    )


def _score(sess: Session) -> str:
    earned = _earned(sess)
    total = sum(ACHIEVEMENTS[code][1] for code in earned)
    lines = [f"Crawler {sess.user}", f"points    {total}"]
    if not earned:
        lines.append("no stamps yet. the jacket stays on the bench.")
    for code in earned:
        lines.append(f"{code}  {ACHIEVEMENTS[code][0]}")
    return _term("\n".join(lines))


def _quest(sess: Session) -> str:
    earned = set(_earned(sess))
    if "ORIENT-2" in earned:
        return _term(_read("quest-done.txt", sess))
    if "ORIENT-1" in earned:
        return _term(_read("quest-notes.txt", sess))
    return _term(_read("quest-pamphlet.txt", sess))


def _inventory(sess: Session) -> str:
    earned = set(_earned(sess))
    lines = ["one (1) sense of direction, currently overdrawn"]
    if "ORIENT-1" in earned:
        lines.append("a jacket three sizes off, sleeves arguing with each other")
    if "ORIENT-2" in earned:
        lines.append("a coupon for the vending machine, which is out of order")
    if not earned:
        lines.append("no prizes. the bench is relieved.")
    return _term("\n".join(lines))


def _news(sess: Session) -> str:
    return _term(_read("MOTD.txt", sess) + "\n\n" + _read("NEWS.txt", sess))


def dispatch(sess: Session, cmd: str, args: list[str]) -> str:
    if cmd in _NOT_A_SHELL:
        return "not a shell. the booth files that under cute. try look.\r\n"
    if cmd not in get_pack("orientation").commands:
        return f"{cmd}: not found\r\n"
    if cmd == "help":
        return _term(_read("help.txt", sess))
    if cmd == "news":
        return _news(sess)
    if cmd == "quest":
        return _quest(sess)
    if cmd == "score":
        return _score(sess)
    if cmd == "look":
        return _look(sess)
    if cmd == "go":
        return _go(sess, args)
    if cmd == "talk":
        return _talk(sess, args)
    if cmd == "inventory":
        return _inventory(sess)
    if cmd == "submit":
        return _submit(sess, args)
    if cmd == "bye":
        return leave(sess)
    return f"{cmd}: not found\r\n"
