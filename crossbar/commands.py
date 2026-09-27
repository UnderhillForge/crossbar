"""Command dispatch and the aliases already on the wire."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable

from crossbar import v7
from crossbar import chainrpc
from crossbar.accounts import (
    RESERVED,
    achievements_for,
    authenticate,
    create_account,
    email_of,
    get_account,
    hash_password,
    record_claim,
    set_account_fields,
    wallet_of,
    wallet_owner,
)
from crossbar.config import MAX_HISTORY
from crossbar.lobby import (
    LOGIN_LINE,
    MOTD_LINE,
    banner,
    header_line,
    help_text,
    login_prompt,
    motd_text,
    news_text,
    pad_card,
    pad_hosts_text,
    pad_map_text,
)
from crossbar import orientation
from crossbar import site
from crossbar.packs import PACKS, get_pack, hosts_listing
from crossbar.session import Session, live_sessions, prompt_for, valid_name

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VERBS = frozenset({"guest", "new", "login"})
_HELP_TOPICS = {
    "?": "this list",
    "help": "this list",
    "hosts": "name, title, baud ceiling, state",
    "finger": "a name on the wire",
    "who": "other live sessions",
    "connect": "open a circuit. orientation is CONNECT BEC",
    "date": "pad clock",
    "motd": "message of the day",
    "news": "pad bulletin",
    "full": "baud, handle, destination",
    "status": "baud, handle, destination",
    "bye": "back along the circuit",
    "logout": "clear your name",
    "bec": "orientation circuit, 1200, outside plant",
}


def _arrived(sess: Session) -> str:
    sess.phase = "shell"
    sess.pending_handle = ""
    sess.pending_password = ""
    sess.host = "grayline"
    sess.previous_host = ""
    return f"{pad_card(sess.user or 'guest')}{prompt_for(sess)}"


def _clear_pending(sess: Session) -> None:
    sess.pending_handle = ""
    sess.pending_password = ""


def login_line(sess: Session, raw: str) -> str:
    phase = sess.phase
    if phase == "password":
        return _login_password(sess, raw)
    if phase == "new_handle":
        return _new_handle(sess, raw)
    if phase == "new_password":
        return _new_password(sess, raw)
    if phase == "new_confirm":
        return _new_confirm(sess, raw)
    if phase == "new_email":
        return _new_email(sess, raw)
    return _login_name(sess, raw)


def _login_name(sess: Session, raw: str) -> str:
    name = raw.strip().lower()
    if name in {"", "guest"}:
        if not site.guest_enabled():
            return f"logon closed\r\n{login_prompt()}"
        sess.user = "guest"
        return _arrived(sess)
    if name == "new":
        if not site.registration_enabled():
            return f"registration is closed\r\n{login_prompt()}"
        _clear_pending(sess)
        sess.phase = "new_handle"
        return "handle: "
    if name in RESERVED or name in _VERBS:
        return f"reserved\r\n{login_prompt()}"
    if not valid_name(name):
        return f"{LOGIN_LINE}\r\nLOGON: "
    if get_account(name) is None:
        return f"{name}: no such user\r\nLOGON: "
    sess.pending_handle = name
    sess.phase = "password"
    return "PASSWORD: "


def _login_password(sess: Session, raw: str) -> str:
    if raw == "":
        return "PASSWORD: "
    handle = sess.pending_handle
    if not authenticate(handle, raw):
        sess.phase = "login"
        _clear_pending(sess)
        return f"IDENTIFICATION NOT RECOGNIZED\r\n{login_prompt()}"
    sess.user = handle
    return _arrived(sess)


def _new_handle(sess: Session, raw: str) -> str:
    name = raw.strip().lower()
    if name == "":
        return "handle: "
    if name in RESERVED or name in _VERBS:
        return "reserved\r\nhandle: "
    if not valid_name(name):
        return "handle: a letter, then up to 11 letters or digits\r\nhandle: "
    if get_account(name) is not None:
        return "handle taken\r\nhandle: "
    sess.pending_handle = name
    sess.phase = "new_password"
    return "PASSWORD: "


def _new_password(sess: Session, raw: str) -> str:
    if len(raw) < 4:
        return "password: at least 4 characters\r\nPASSWORD: "
    sess.pending_password = raw
    sess.phase = "new_confirm"
    return "confirm: "


def _new_confirm(sess: Session, raw: str) -> str:
    if raw != sess.pending_password:
        sess.pending_password = ""
        sess.phase = "new_password"
        return "passwords differ\r\nPASSWORD: "
    sess.phase = "new_email"
    return "email: "


def _new_email(sess: Session, raw: str) -> str:
    email = raw.strip()
    if not _EMAIL.fullmatch(email) or len(email) > 254:
        return "email: name@host\r\nemail: "
    try:
        create_account(sess.pending_handle, sess.pending_password, email)
    except ValueError as exc:
        _clear_pending(sess)
        sess.phase = "new_handle"
        reason = "reserved" if str(exc) == "reserved" else "handle taken"
        return f"{reason}\r\nhandle: "
    sess.user = sess.pending_handle
    return _arrived(sess)


def cmd_help(sess: Session, args: list[str]) -> str:
    pack = get_pack(sess.host)
    if pack.name == "tymnet":
        return (
            "TYMNET\r\n"
            "\r\n"
            "  help                  this pad\r\n"
            "  hosts                 circuits you can open\r\n"
            "  connect <host>        open a virtual circuit\r\n"
            "  finger [name]         a live session\r\n"
            "  who                   other live sessions\r\n"
            "  bye, g                back to grayline\r\n"
        )
    if pack.name == "terminal-addiction":
        return "NO CARRIER\r\n"
    if args:
        topic = args[0].lower()
        blurb = _HELP_TOPICS.get(topic)
        if blurb is None:
            return f"help: {topic}: not found\r\n"
        return f"{topic.upper()}  {blurb}\r\n"
    return help_text()


def cmd_who(sess: Session, args: list[str]) -> str:
    names: list[str] = []
    for other in live_sessions():
        if other.sid != sess.sid and other.user:
            names.append(other.user)
    names.sort()
    if not names:
        return "no one else on the wire\r\n"
    return "".join(f"{name}\r\n" for name in names)


def cmd_motd(sess: Session, args: list[str]) -> str:
    return motd_text()


def cmd_news(sess: Session, args: list[str]) -> str:
    if sess.host == "grayline" and not site.verb_enabled("news"):
        return "news: not found\r\n"
    return news_text()


def cmd_date(sess: Session, args: list[str]) -> str:
    return v7._now().strftime("%a %b %d %H:%M:%S %Z %Y") + "\r\n"


def _ident(sess: Session) -> str:
    handle = sess.user or "guest"
    return "GUEST" if handle.lower() == "guest" else handle


def cmd_status(sess: Session, args: list[str]) -> str:
    return (
        "NODE GL-01\r\n"
        f"IDENT {_ident(sess)}\r\n"
        "DEST PAD\r\n"
        f"BAUD {sess.baud_now}\r\n"
    )


def cmd_full(sess: Session, args: list[str]) -> str:
    who = _ident(sess)
    baud = str(sess.baud_now)
    # No chain RPC on this pad. Omit height rather than invent one.
    def pane(title: str, rows: list[str]) -> str:
        width = 31
        top = "┌ " + title + " " + "─" * (width - len(title) - 3) + "┐"
        body = ["│" + row.ljust(width) + "│" for row in rows]
        bot = "└" + "─" * width + "┘"
        return "\r\n".join([top, *body, bot])

    text = "\r\n".join(
        [
            pane("PAD", [" NODE GL-01", f" BAUD {baud}"]),
            pane("IDENT", [f" {who}"]),
            pane("GATES", [" BEC 1200 UP", " TA 2400 OFFLINE", " TYMNET UP"]),
            "NEWS UNAVAILABLE",
        ]
    )
    return text + "\r\n"


def cmd_map(sess: Session, args: list[str]) -> str:
    if sess.host == "grayline" and not site.verb_enabled("map"):
        return "map: not found\r\n"
    return pad_map_text()


def cmd_ls(sess: Session, args: list[str]) -> str:
    from crossbar.lobby import ls_text

    return ls_text()


def cmd_mail(sess: Session, args: list[str]) -> str:
    return "no letters.\r\n"


def cmd_verify(sess: Session, args: list[str]) -> str:
    return "verification is dark\r\n"


def cmd_login(sess: Session, args: list[str]) -> str:
    return f"already logged in as {sess.user}\r\nlogout to leave\r\n"


def cmd_already(sess: Session, args: list[str]) -> str:
    if sess.host == "grayline":
        line = MOTD_LINE
    else:
        line = get_pack(sess.host).here_line
    return f"already connected to {sess.host}\r\n{line}\r\n"


def cmd_messages(sess: Session, args: list[str]) -> str:
    return "no messages yet.\r\n"


def cmd_files(sess: Session, args: list[str]) -> str:
    return "no files yet.\r\n"


def hop(sess: Session, dest: str) -> str:
    # In-process only. sess.host changes; nothing dials out.
    from crossbar.session import push_hop

    ceiling = get_pack(dest).baud_max
    push_hop(sess, dest, ceiling)
    return get_pack(dest).banner


def pop_to_previous(sess: Session) -> str:
    from crossbar.session import pop_hop

    host = pop_hop(sess)
    return get_pack(host).banner


def _return_host(sess: Session) -> str:
    dest = sess.previous_host
    pack = PACKS.get(dest)
    if pack is not None and pack.up and dest != sess.host:
        return dest
    return "grayline"


def cmd_bye(sess: Session, args: list[str]) -> str:
    pack = get_pack(sess.host)
    if pack.name == "orientation":
        return orientation.leave(sess)
    if pack.name in {"bec", "bec-mf"}:
        return v7.hangup(sess)
    if pack.name == "grayline":
        return "already on grayline\r\n"
    text = pop_to_previous(sess)
    if sess.host == "grayline":
        return pad_card(sess.user or "guest")
    return text


def cmd_doors(sess: Session, args: list[str]) -> str:
    return v7.doors_text()


def cmd_door_one(sess: Session, args: list[str]) -> str:
    return v7.enter(sess)


def _fmt_idle(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _grant_count(name: str) -> int:
    owners = []
    if get_account(name) is not None:
        owners.append(f"acct:{name}")
    total = 0
    for owner in owners:
        total += len(achievements_for(owner))
    return total


def _format_fingers(rows: list[Session]) -> str:
    blocks: list[str] = []
    for person in rows:
        name = person.user or "guest"
        shown = "GUEST" if name.lower() == "guest" else name
        dest = "PAD" if person.host in {"grayline", "tymnet"} else person.host
        blocks.append(
            f"IDENT    {shown}\r\n"
            f"DEST     {dest}\r\n"
            f"GRANTS   {_grant_count(name)}"
        )
    return "\r\n\r\n".join(blocks) + "\r\n"


def cmd_finger(sess: Session, args: list[str]) -> str:
    if sess.host == "grayline" and not site.verb_enabled("finger"):
        return "finger: not found\r\n"
    if not args:
        return _format_fingers([sess])
    name = args[0].lower()
    if name in {"bec", "big-evil"}:
        return "CIRCUIT  BEC\r\nBAUD     1200\r\nSTATE    UP\r\n"
    if name in {"sysop", "admin"}:
        return f"IDENT    {name}\r\nMAIL     not accepting mail\r\n"
    if not valid_name(name):
        return f"finger: {name}: not found\r\n"
    found = [other for other in live_sessions() if other.user == name]
    if not found:
        return f"finger: {name}: not on the wire\r\n"
    return _format_fingers(found)


def cmd_hosts(sess: Session, args: list[str]) -> str:
    if get_pack(sess.host).name == "grayline":
        if args and args[0].upper() in {"/T", "T"}:
            if not site.verb_enabled("map"):
                return "hosts: not found\r\n"
            return pad_map_text()
        return pad_hosts_text()
    return hosts_listing(sess.host)


_CONNECT_ALIAS = {
    "big-evil": "bec",
    "ta": "terminal-addiction",
    "gl": "grayline",
}


def cmd_connect(sess: Session, args: list[str]) -> str:
    if len(args) != 1:
        return "usage: connect <host>\r\n"
    name = _CONNECT_ALIAS.get(args[0].lower(), args[0].lower())
    if name == "orientation":
        return "orientation: not found\r\n"
    if name == "grayline":
        if sess.host == "grayline":
            return cmd_already(sess, [])
        from crossbar.config import T1_BAUD
        from crossbar.session import ensure_path, pop_hop

        ensure_path(sess)
        guard = 0
        while sess.host != "grayline" and len(sess.hops) > 1 and guard < 8:
            pop_hop(sess)
            guard += 1
        if sess.host != "grayline":
            sess.host = "grayline"
            sess.hops = ["grayline"]
            sess.baud_stack = [T1_BAUD]
            sess.baud_now = T1_BAUD
        return pad_card(sess.user or "guest")
    known = {host.name: host for host in get_pack(sess.host).hosts}
    host = known.get(name)
    if host is None:
        return f"{name}: not found\r\n"
    state = site.host_state(name, host.up and (name not in PACKS or get_pack(name).up), host.up)
    offline = state != "UP" or name not in PACKS
    if offline:
        if name == "terminal-addiction":
            return "DIALING 2400...\r\nNO CARRIER\r\n"
        return f"{name} is dark\r\n"
    if name == sess.host:
        return cmd_already(sess, [])
    if name == "bec":
        return "DIALING 1200...\r\n" + v7.enter(sess)
    return hop(sess, name)


def _profile_menu(sess: Session) -> str:
    handle = sess.user or ""
    ps1 = wallet_of(handle) or "none"
    lines = [
        "PROFILE",
        f"HANDLE  {handle}",
        f"EMAIL   {email_of(handle) or 'none'}",
        f"PS1     {ps1}",
    ]
    if ps1 != "none":
        try:
            extra = chainrpc.balance_line(ps1)
        except Exception:
            extra = ""
        if extra:
            lines.append(extra.rstrip("\r\n"))
    lines.extend(["", "1 EMAIL", "2 PASSWORD", "3 PS1", "Q BACK", ""])
    return "\r\n".join(lines)


def cmd_profile(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "profile_config: not found\r\n"
    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    sess.phase = "profile"
    sess.pending_password = ""
    return _profile_menu(sess)


def profile_line(sess: Session, raw: str) -> str:
    text = raw.strip()
    phase = sess.phase
    if phase == "profile":
        choice = text.lower()
        if choice in {"", "q", "back"}:
            if choice in {"q", "back"}:
                sess.phase = "shell"
                return ""
            return _profile_menu(sess)
        if choice in {"1", "email"}:
            sess.phase = "profile_email"
            return "EMAIL\r\n"
        if choice in {"2", "password"}:
            sess.phase = "profile_password"
            return "PASSWORD\r\n"
        if choice in {"3", "ps1"}:
            sess.phase = "profile_ps1"
            return "PS1\r\n"
        return "1 EMAIL\r\n2 PASSWORD\r\n3 PS1\r\nQ BACK\r\n"
    if phase == "profile_email":
        email = text
        if not _EMAIL.fullmatch(email) or len(email) > 254:
            return "email: name@host\r\nEMAIL\r\n"
        set_account_fields(sess.user or "", email=email)
        sess.phase = "profile"
        return "email saved\r\n" + _profile_menu(sess)
    if phase == "profile_password":
        if len(text) < 4:
            return "password: at least 4 characters\r\nPASSWORD\r\n"
        sess.pending_password = text
        sess.phase = "profile_password2"
        return "CONFIRM\r\n"
    if phase == "profile_password2":
        if text != sess.pending_password:
            sess.pending_password = ""
            sess.phase = "profile_password"
            return "passwords differ\r\nPASSWORD\r\n"
        set_account_fields(sess.user or "", password_hash=hash_password(text), must_change=0)
        sess.pending_password = ""
        sess.phase = "profile"
        return "password saved\r\n" + _profile_menu(sess)
    if phase == "profile_ps1":
        return _link_ps1(sess, text) + _profile_menu(sess)
    sess.phase = "shell"
    return ""


def _link_ps1(sess: Session, text: str) -> str:
    handle = sess.user or ""
    ps1 = chainrpc.normalize_ps1(text)
    if not ps1:
        try:
            ps1 = chainrpc.lookup_name(text)
        except chainrpc.MethodMissing:
            sess.phase = "profile"
            return "name lookup is not on this node yet\r\n"
        except chainrpc.RpcDown:
            sess.phase = "profile"
            return "node did not answer\r\n"
    if not ps1:
        sess.phase = "profile"
        return "ps1 not found\r\n"
    owner = wallet_owner(ps1)
    if owner and owner != handle:
        sess.phase = "profile"
        return "ps1 already linked\r\n"
    try:
        set_account_fields(handle, wallet=ps1)
    except sqlite3.IntegrityError:
        sess.phase = "profile"
        return "ps1 already linked\r\n"
    sess.phase = "profile"
    return "ps1 saved\r\n"


def cmd_claim(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "claim: not found\r\n"
    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    if not args:
        return "claim: usage: claim FLAG_ID\r\n"
    flag_id = args[0]
    ps1 = wallet_of(sess.user)
    if not ps1:
        return "link a wallet first: ps1 link can be found in profile_config\r\n"
    line, txid = chainrpc.claim_flag(flag_id, ps1)
    if line == "claimed" and txid:
        record_claim(sess.user, flag_id, ps1, txid)
        return "claimed\r\n"
    if line == "claimed":
        return "claim refused\r\n"
    return line if line.endswith("\r\n") else line + "\r\n"


def cmd_chain(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "chain: not found\r\n"
    return chainrpc.chain_text()


def cmd_logout(sess: Session, args: list[str]) -> str:
    # Keep sid. The cookie still names this Session; only the person leaves.
    sess.user = None
    sess.host = "grayline"
    sess.history.clear()
    sess.line = ""
    sess.hist_i = None
    sess.draft = ""
    sess.esc = None
    sess.phase = "login"
    sess.pending_handle = ""
    sess.pending_password = ""
    sess.previous_host = ""
    sess.room = ""
    sess.seen_look = False
    return banner() + login_prompt()


COMMANDS: dict[str, Callable[[Session, list[str]], str]] = {
    "help": cmd_help,
    "?": cmd_help,
    "who": cmd_who,
    "motd": cmd_motd,
    "news": cmd_news,
    "date": cmd_date,
    "full": cmd_full,
    "status": cmd_status,
    "map": cmd_map,
    "finger": cmd_finger,
    "whois": cmd_finger,
    "hosts": cmd_hosts,
    "host": cmd_hosts,
    "ls": cmd_ls,
    "dir": cmd_ls,
    "mail": cmd_mail,
    "verify": cmd_verify,
    "connect": cmd_connect,
    "bye": cmd_bye,
    "g": cmd_bye,
    "d": cmd_doors,
    "doors": cmd_doors,
    "1": cmd_door_one,
    "m": cmd_messages,
    "f": cmd_files,
    "login": cmd_login,
    "logout": cmd_logout,
    "exit": cmd_logout,
    "profile_config": cmd_profile,
    "claim": cmd_claim,
    "chain": cmd_chain,
}


def submit(sess: Session) -> str:
    text = sess.line
    sess.line = ""
    sess.hist_i = None
    sess.draft = ""
    if sess.user is None:
        return login_line(sess, text)
    if sess.host == "grayline" and sess.phase.startswith("profile"):
        body = profile_line(sess, text)
        if sess.user is None:
            return body
        return body + prompt_for(sess)
    if sess.host in {"bec", "bec-mf"}:
        if sess.v7_phase != "password" and text.strip():
            if not sess.history or sess.history[-1] != text.strip():
                sess.history.append(text.strip())
            if len(sess.history) > MAX_HISTORY:
                del sess.history[: len(sess.history) - MAX_HISTORY]
        body = v7.on_line(sess, text)
        if sess.v7_phase == "pico":
            return body
        return body + prompt_for(sess)
    stripped = text.strip()
    if not stripped:
        if sess.host == "grayline" and sess.user:
            return header_line(sess.user) + prompt_for(sess)
        return prompt_for(sess)
    if not sess.history or sess.history[-1] != stripped:
        sess.history.append(stripped)
    if len(sess.history) > MAX_HISTORY:
        del sess.history[: len(sess.history) - MAX_HISTORY]
    parts = stripped.split()
    cmd, args = parts[0].lower(), parts[1:]
    pack = get_pack(sess.host)
    if pack.name == "orientation":
        body = orientation.dispatch(sess, cmd, args)
    elif cmd == sess.host:
        body = cmd_already(sess, args)
    else:
        handler = COMMANDS.get(cmd)
        if handler is None or cmd not in pack.commands:
            body = f"{cmd}: not found\r\n"
        else:
            body = handler(sess, args)
    if sess.user is None:
        return body
    return body + prompt_for(sess)
