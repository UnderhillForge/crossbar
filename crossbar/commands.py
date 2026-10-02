"""Command dispatch and the aliases already on the wire."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime

from crossbar import v7
from crossbar import chainrpc
from crossbar.accounts import (
    RESERVED,
    achievements_for,
    authenticate,
    claim_ranks,
    claim_recorded,
    create_account,
    email_of,
    get_account,
    hash_password,
    list_accounts,
    note_of,
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
    return_to_pad,
)
from crossbar import site
from crossbar.packs import PACKS, get_pack, hosts_listing
from crossbar.session import Session, live_sessions, prompt_for, valid_name

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VERBS = frozenset({"guest", "new", "login"})
_HELP_TOPICS = {
    "?": "this list",
    "help": "this list",
    "hosts": "name, title, baud ceiling, state",
    "finger": "who is on the wire, or a handle",
    "who": "other live sessions",
    "connect": "open a circuit. orientation is CONNECT BEC",
    "date": "pad clock",
    "motd": "message of the day",
    "news": "pad bulletin",
    "mail": "letters: list read send reply fwd del archive",
    "groups": "forums: list new headers read post next",
    "wall": "last 10 wall lines; write one line",
    "full": "baud, handle, destination",
    "status": "baud, handle, destination",
    "bye": "back along the circuit",
    "clear": "clear the screen",
    "clr": "clear the screen",
    "logout": "clear your name",
    "bec": "orientation circuit, 1200, outside plant",
    "claim": "claim a flag for the linked ps1",
    "pschain": "nodes, height, difficulty, hash, health",
    "leaders": "top 20 by accepted claims",
    "user_list": "accounts, rank, last login",
}


def _arrived(sess: Session) -> str:
    from crossbar import mail

    sess.phase = "shell"
    sess.pending_handle = ""
    sess.pending_password = ""
    sess.host = "grayline"
    sess.previous_host = ""
    sess.login_at = time.time()
    card = pad_card(sess.user or "guest")
    hint = mail.unread_notice(sess.user)
    return f"{card}{hint}{prompt_for(sess)}"


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


def cmd_clear(sess: Session, args: list[str]) -> str:
    """Wipe the terminal; submit() still appends the pad prompt."""
    return "\x1b[2J\x1b[H"


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
            "NEWS  LOCAL BULLETIN",
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


def _mail_gate(sess: Session) -> str | None:
    if sess.host != "grayline":
        return "mail: not found\r\n"
    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    return None


def _clear_mail_draft(sess: Session) -> None:
    sess.mail_to = ""
    sess.mail_subject = ""
    sess.mail_body_lines = []
    sess.mail_reply_to = None


def _mail_subject_shown(subject: str) -> str:
    text = (subject or "").strip()
    return text if text else "(no subject)"


def _mail_when(created: str) -> str:
    text = str(created or "").strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:16] if text else "*"
    return moment.astimezone().strftime("%d %b %H:%M")


def _mail_help() -> str:
    return (
        "MAIL                 inbox summary\r\n"
        "MAIL LIST [folder]   inbox | sent | archive\r\n"
        "MAIL READ <id>\r\n"
        "MAIL SEND <handle> [subject…]\r\n"
        "MAIL REPLY <id>\r\n"
        "MAIL FWD <id> <handle>\r\n"
        "MAIL DEL <id>…\r\n"
        "MAIL ARCHIVE <id>…\r\n"
        "MAIL HELP\r\n"
        "Compose ends with . alone. Q or ^C cancels.\r\n"
        "Blank subject is allowed → (no subject).\r\n"
    )


def _mail_re_subject(subject: str) -> str:
    text = (subject or "").strip()
    if not text:
        return "Re: (no subject)"
    if text.lower().startswith("re:"):
        return text
    return f"Re: {text}"


def _mail_fwd_subject(subject: str) -> str:
    text = (subject or "").strip()
    if not text:
        return "Fwd: (no subject)"
    if text.lower().startswith("fwd:"):
        return text
    return f"Fwd: {text}"


def _mail_quote(body: str) -> list[str]:
    lines = ["", "--- original ---"]
    for raw in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        chunk = raw
        while len(chunk) > 70:
            lines.append("> " + chunk[:70])
            chunk = chunk[70:]
        lines.append("> " + chunk)
    return lines


def _mail_begin_compose(
    sess: Session,
    *,
    to: str,
    subject: str,
    body_lines: list[str] | None = None,
    reply_to: int | None = None,
) -> str:
    from crossbar import mail

    try:
        mail._assert_recipient(sess.user or "", to)
    except ValueError as exc:
        return f"{exc}\r\n"
    sess.mail_to = to.strip().lower()
    sess.mail_subject = subject.replace("\x00", "")[: mail.SUBJECT_MAX]
    sess.mail_body_lines = list(body_lines or [])
    sess.mail_reply_to = reply_to
    sess.phase = "mail_body"
    return (
        f"Compose to {sess.mail_to}. End with . on a line by itself. "
        "Q alone cancels.\r\n"
    )


def _mail_list(sess: Session, folder: str) -> str:
    from crossbar import mail

    folder = (folder or "inbox").lower()
    if folder not in {"inbox", "sent", "archive"}:
        return "usage: MAIL LIST [inbox|sent|archive]\r\n"
    rows = mail.list_letters(sess.user or "", folder)
    unread = mail.unread_count(sess.user or "")
    inbox_n = len(mail.list_letters(sess.user or "", "inbox"))
    lines = [
        f"MAILBOX  {sess.user}  ·  {unread} unread  ·  {inbox_n} inbox",
        " ID   FROM         WHEN         SUBJECT",
    ]
    if not rows:
        lines.append("(empty)")
    for letter in rows:
        who = letter.sender if folder != "sent" else letter.to_list
        lines.append(
            f" {letter.id:<4} {who:<12} {_mail_when(letter.created):<12} "
            f"{_mail_subject_shown(letter.subject)}"
        )
    lines.append("Type MAIL READ <id>  ·  MAIL HELP for verbs")
    return "\r\n".join(lines) + "\r\n"


def _mail_read(sess: Session, letter_id: int) -> str:
    from crossbar import mail

    letter = mail.get_letter(sess.user or "", letter_id)
    if letter is None:
        return "no such letter\r\n"
    mail.mark_read(sess.user or "", letter_id)
    body = letter.body.replace("\n", "\r\n")
    return (
        f"Letter {letter.id}\r\n"
        f"From: {letter.sender}\r\n"
        f"To:   {letter.to_list}\r\n"
        f"Date: {_mail_when(letter.created)}\r\n"
        f"Subj: {_mail_subject_shown(letter.subject)}\r\n"
        "────────────────────────────────────────\r\n"
        f"{body}\r\n"
        "────────────────────────────────────────\r\n"
        f"REPLY {letter.id}  ·  FWD {letter.id} <handle>  ·  "
        f"DEL {letter.id}  ·  ARCHIVE {letter.id}\r\n"
    )


def _mail_begin_send(sess: Session, to: str, subject: str | None) -> str:
    if subject is not None:
        return _mail_begin_compose(sess, to=to, subject=subject)
    from crossbar import mail

    try:
        mail._assert_recipient(sess.user or "", to)
    except ValueError as exc:
        return f"{exc}\r\n"
    sess.mail_to = to.strip().lower()
    sess.mail_reply_to = None
    sess.mail_body_lines = []
    sess.mail_subject = ""
    sess.phase = "mail_subject"
    return ""


def _mail_reply(sess: Session, letter_id: int) -> str:
    from crossbar import mail

    letter = mail.get_letter(sess.user or "", letter_id)
    if letter is None:
        return "no such letter\r\n"
    if letter.folder not in {"inbox", "archive"} or letter.sender == (sess.user or ""):
        return "cannot reply to this letter\r\n"
    if letter.sender in RESERVED or letter.sender == mail.SYSTEM_SENDER:
        return f"{letter.sender} is not accepting mail\r\n"
    mail.mark_read(sess.user or "", letter_id)
    return _mail_begin_compose(
        sess,
        to=letter.sender,
        subject=_mail_re_subject(letter.subject),
        body_lines=_mail_quote(letter.body),
        reply_to=letter.id,
    )


def _mail_fwd(sess: Session, letter_id: int, handle: str) -> str:
    from crossbar import mail

    letter = mail.get_letter(sess.user or "", letter_id)
    if letter is None:
        return "no such letter\r\n"
    mail.mark_read(sess.user or "", letter_id)
    block = [
        "",
        f"--- forwarded message from {letter.sender} ---",
        f"Date: {_mail_when(letter.created)}",
        f"Subj: {_mail_subject_shown(letter.subject)}",
        "",
        *letter.body.replace("\r\n", "\n").replace("\r", "\n").split("\n"),
    ]
    return _mail_begin_compose(
        sess,
        to=handle,
        subject=_mail_fwd_subject(letter.subject),
        body_lines=block,
        reply_to=None,
    )


def _mail_archive(sess: Session, ids: list[str]) -> str:
    from crossbar import mail

    moved = 0
    for tok in ids:
        if mail.archive(sess.user or "", int(tok)):
            moved += 1
    if moved == 0:
        return "no such letter\r\n"
    return f"archived {moved}\r\n"


def _mail_finish_send(sess: Session) -> str:
    from crossbar import mail

    body = "\n".join(sess.mail_body_lines)
    to = sess.mail_to
    subject = sess.mail_subject
    reply_to = sess.mail_reply_to
    try:
        mid = mail.send(
            sender=sess.user or "",
            to=to,
            subject=subject,
            body=body,
            in_reply_to=reply_to,
        )
    except ValueError as exc:
        return f"{exc}\r\n"
    finally:
        _clear_mail_draft(sess)
        sess.phase = "shell"
    return f"sent {mid} to {to}\r\n"


def mail_line(sess: Session, raw: str) -> str:
    text = raw
    if sess.phase == "mail_subject":
        stripped = text.strip()
        if stripped.upper() == "Q":
            _clear_mail_draft(sess)
            sess.phase = "shell"
            return "cancelled\r\n"
        sess.mail_subject = stripped.replace("\x00", "")
        from crossbar import mail

        if len(sess.mail_subject) > mail.SUBJECT_MAX:
            sess.mail_subject = ""
            return "subject too long\r\n"
        sess.phase = "mail_body"
        return (
            f"Compose to {sess.mail_to}. End with . on a line by itself. "
            "Q alone cancels.\r\n"
        )
    if sess.phase == "mail_body":
        stripped = text.strip()
        if stripped.upper() == "Q":
            _clear_mail_draft(sess)
            sess.phase = "shell"
            return "cancelled\r\n"
        if stripped == ".":
            return _mail_finish_send(sess)
        from crossbar import mail

        tentative = sess.mail_body_lines + [text.rstrip("\r\n")]
        joined = "\n".join(tentative)
        if len(tentative) > mail.BODY_MAX_LINES or len(joined) > mail.BODY_MAX_CHARS:
            return "body too long\r\n"
        sess.mail_body_lines.append(text.rstrip("\r\n"))
        return ""
    sess.phase = "shell"
    _clear_mail_draft(sess)
    return ""


def cmd_mail(sess: Session, args: list[str]) -> str:
    blocked = _mail_gate(sess)
    if blocked:
        return blocked
    if not args:
        return _mail_list(sess, "inbox")
    verb = args[0].lower()
    rest = args[1:]
    if verb in {"help", "?"}:
        return _mail_help()
    if verb == "list":
        folder = rest[0] if rest else "inbox"
        return _mail_list(sess, folder)
    if verb == "read":
        if len(rest) != 1 or not rest[0].isdigit():
            return "usage: MAIL READ <id>\r\n"
        return _mail_read(sess, int(rest[0]))
    if verb == "send":
        if not rest:
            return "usage: MAIL SEND <handle> [subject]\r\n"
        to = rest[0]
        subject = " ".join(rest[1:]) if len(rest) > 1 else None
        return _mail_begin_send(sess, to, subject)
    if verb in {"del", "delete", "rm"}:
        if not rest or not all(tok.isdigit() for tok in rest):
            return "usage: MAIL DEL <id>…\r\n"
        from crossbar import mail

        removed = 0
        for tok in rest:
            if mail.soft_delete(sess.user or "", int(tok)):
                removed += 1
        if removed == 0:
            return "no such letter\r\n"
        return f"deleted {removed}\r\n"
    if verb == "reply":
        if len(rest) != 1 or not rest[0].isdigit():
            return "usage: MAIL REPLY <id>\r\n"
        return _mail_reply(sess, int(rest[0]))
    if verb in {"fwd", "forward"}:
        if len(rest) != 2 or not rest[0].isdigit():
            return "usage: MAIL FWD <id> <handle>\r\n"
        return _mail_fwd(sess, int(rest[0]), rest[1])
    if verb == "archive":
        if not rest or not all(tok.isdigit() for tok in rest):
            return "usage: MAIL ARCHIVE <id>…\r\n"
        return _mail_archive(sess, rest)
    return "usage: MAIL HELP\r\n"


def _wall_when(created: str) -> str:
    return _mail_when(created)


def cmd_wall(sess: Session, args: list[str]) -> str:
    from crossbar import wall

    if sess.host != "grayline":
        return "wall: not found\r\n"
    if not args:
        posts = wall.list_posts()
        lines = ["WALL  (last 10)"]
        if not posts:
            lines.append("(empty)")
        else:
            for handle, body, created in reversed(posts):
                if not handle:
                    lines.append(body)
                else:
                    lines.append(f"{_wall_when(created)}  {handle}: {body}")
        lines.append("WALL <text>  write one line (registered)")
        return "\r\n".join(lines) + "\r\n"
    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    text = " ".join(args)
    try:
        wall.post(sess.user, text)
    except ValueError as exc:
        return f"{exc}\r\n"
    return "posted\r\n" + cmd_wall(sess, [])


def _clear_group_draft(sess: Session) -> None:
    sess.group_subject = ""
    sess.group_body_lines = []


def _groups_help() -> str:
    return (
        "GROUPS                 list groups\r\n"
        "GROUPS LIST            same\r\n"
        "GROUPS NEW             unread since you last read\r\n"
        "GROUPS <name>          select a group\r\n"
        "GROUPS HEADERS [n]     recent headers (default 20)\r\n"
        "GROUPS READ <n>        read article number\r\n"
        "GROUPS NEXT / PREV     move in current group\r\n"
        "GROUPS POST [subject]  post to current group\r\n"
        "GROUPS HELP\r\n"
        "NEWS is the system bulletin; GROUPS are forums.\r\n"
    )


def _groups_subject_shown(subject: str) -> str:
    text = (subject or "").strip()
    return text if text else "(no subject)"


def _groups_list(sess: Session) -> str:
    from crossbar import groups

    rows = groups.list_groups(for_handle=sess.user or "")
    lines = ["GROUPS"]
    if not rows:
        lines.append("(none)")
    else:
        lines.append(f"{'name':<22} {'high':>4} {'new':>4}  description")
        for row in rows:
            unread = row.unread if sess.user and sess.user != "guest" else 0
            lines.append(
                f"{row.name:<22} {row.high:>4} {unread:>4}  {row.description}"
            )
    if sess.group_name:
        lines.append(f"current: {sess.group_name}")
    lines.append("GROUPS HELP for verbs")
    return "\r\n".join(lines) + "\r\n"


def _groups_select(sess: Session, name: str) -> str:
    from crossbar import groups

    info = groups.get_group(name)
    if info is None:
        return "no such group\r\n"
    sess.group_name = info.name
    sess.group_art = info.high
    return (
        f"Group {info.name} ({info.description})\r\n"
        f"articles: {info.high}  policy: {info.post_policy}\r\n"
        "GROUPS HEADERS · GROUPS READ <n> · GROUPS POST\r\n"
    )


def _groups_headers(sess: Session, limit: int | None) -> str:
    from crossbar import groups

    if not sess.group_name:
        return "no group selected\r\n"
    cap = groups.HEADERS_DEFAULT if limit is None else limit
    try:
        rows = groups.headers(sess.group_name, limit=cap)
    except ValueError as exc:
        return f"{exc}\r\n"
    lines = [f"{sess.group_name}  headers (newest first)"]
    if not rows:
        lines.append("(empty)")
    else:
        for row in reversed(rows):
            lines.append(
                f"{row.number:>4}  {_wall_when(row.date_sent)}  "
                f"{row.from_handle:<12} {_groups_subject_shown(row.subject)}"
            )
    return "\r\n".join(lines) + "\r\n"


def _groups_new(sess: Session) -> str:
    from crossbar import groups

    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    rows = groups.unread_headers(sess.user)
    lines = ["GROUPS NEW  (unread)"]
    if not rows:
        lines.append("(no new articles)")
        return "\r\n".join(lines) + "\r\n"
    current = ""
    for group_name, row in rows:
        if group_name != current:
            current = group_name
            lines.append(current)
        lines.append(
            f"{row.number:>4}  {_wall_when(row.date_sent)}  "
            f"{row.from_handle:<12} {_groups_subject_shown(row.subject)}"
        )
    lines.append("GROUPS <name> then GROUPS READ <n>")
    return "\r\n".join(lines) + "\r\n"


def _groups_show_article(sess: Session, article) -> str:
    from crossbar import groups

    sess.group_art = article.number
    if sess.user and sess.user != "guest":
        groups.mark_read(sess.user, article.group_name, article.number)
    body = article.body.replace("\n", "\r\n")
    return (
        f"Article {article.number} in {article.group_name}\r\n"
        f"From: {article.from_handle}\r\n"
        f"Date: {_wall_when(article.date_sent)}\r\n"
        f"Subj: {_groups_subject_shown(article.subject)}\r\n"
        f"Message-ID: {article.message_id}\r\n"
        "────────────────────────────────────────\r\n"
        f"{body}\r\n"
    )


def _groups_read(sess: Session, number: int) -> str:
    from crossbar import groups

    if not sess.group_name:
        return "no group selected\r\n"
    article = groups.get_article(sess.group_name, number)
    if article is None:
        return "no such article\r\n"
    return _groups_show_article(sess, article)


def _groups_next_prev(sess: Session, direction: str) -> str:
    from crossbar import groups

    if not sess.group_name:
        return "no group selected\r\n"
    if direction == "next":
        article = groups.next_article(sess.group_name, sess.group_art)
        if article is None and sess.group_art == 0:
            article = groups.next_article(sess.group_name, 0)
    else:
        article = groups.prev_article(sess.group_name, sess.group_art or 10**9)
    if article is None:
        return "no more articles\r\n"
    return _groups_show_article(sess, article)


def _groups_begin_post(sess: Session, subject: str | None) -> str:
    from crossbar import groups

    if not sess.group_name:
        return "no group selected\r\n"
    info = groups.get_group(sess.group_name)
    if info is None:
        return "no such group\r\n"
    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    if info.post_policy == "sysop":
        return "sysop only\r\n"
    if info.post_policy == "readonly":
        return "read only\r\n"
    sess.group_body_lines = []
    if subject is None:
        sess.group_subject = ""
        sess.phase = "group_subject"
        return ""
    sess.group_subject = subject
    sess.phase = "group_body"
    return (
        f"Compose to {sess.group_name}. "
        "End with . on a line by itself. Q alone cancels.\r\n"
    )


def _groups_finish_post(sess: Session) -> str:
    from crossbar import groups

    body = "\n".join(sess.group_body_lines)
    subject = sess.group_subject
    group = sess.group_name
    try:
        number = groups.post(
            group=group,
            from_handle=sess.user or "",
            subject=subject,
            body=body,
        )
    except ValueError as exc:
        return f"{exc}\r\n"
    finally:
        _clear_group_draft(sess)
        sess.phase = "shell"
    sess.group_art = number
    if sess.user and sess.user != "guest":
        groups.mark_read(sess.user, group, number)
    return f"posted {number} to {group}\r\n"


def group_line(sess: Session, raw: str) -> str:
    text = raw
    if sess.phase == "group_subject":
        stripped = text.strip()
        if stripped.upper() == "Q":
            _clear_group_draft(sess)
            sess.phase = "shell"
            return "cancelled\r\n"
        sess.group_subject = stripped
        sess.phase = "group_body"
        return (
            f"Compose to {sess.group_name}. "
            "End with . on a line by itself. Q alone cancels.\r\n"
        )
    if sess.phase == "group_body":
        stripped = text.strip()
        if stripped.upper() == "Q" and not sess.group_body_lines:
            _clear_group_draft(sess)
            sess.phase = "shell"
            return "cancelled\r\n"
        if stripped == ".":
            return _groups_finish_post(sess)
        if stripped.upper() == "Q":
            _clear_group_draft(sess)
            sess.phase = "shell"
            return "cancelled\r\n"
        from crossbar import groups as groups_mod

        tentative = sess.group_body_lines + [text.rstrip("\r\n")]
        if len(tentative) > groups_mod.BODY_MAX_LINES:
            return "body too long\r\n"
        if sum(len(line) + 1 for line in tentative) > groups_mod.BODY_MAX_CHARS:
            return "body too long\r\n"
        sess.group_body_lines.append(text.rstrip("\r\n"))
        return ""
    return ""


def cmd_groups(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "groups: not found\r\n"
    if not args or args[0].lower() in {"list", "ls"}:
        return _groups_list(sess)
    verb = args[0].lower()
    rest = args[1:]
    if verb in {"help", "?"}:
        return _groups_help()
    if verb == "new":
        return _groups_new(sess)
    if verb == "group" and rest:
        return _groups_select(sess, rest[0])
    if verb == "headers":
        limit = None
        if rest:
            if not rest[0].isdigit():
                return "usage: GROUPS HEADERS [n]\r\n"
            limit = int(rest[0])
        return _groups_headers(sess, limit)
    if verb == "read":
        if not rest or not rest[0].isdigit():
            return "usage: GROUPS READ <n>\r\n"
        return _groups_read(sess, int(rest[0]))
    if verb == "next":
        return _groups_next_prev(sess, "next")
    if verb in {"prev", "previous"}:
        return _groups_next_prev(sess, "prev")
    if verb == "post":
        subject = " ".join(rest) if rest else None
        return _groups_begin_post(sess, subject)
    # GROUPS grayline.general
    if "." in verb or verb.startswith("grayline"):
        return _groups_select(sess, args[0])
    return "usage: GROUPS HELP\r\n"


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
    if pack.name in {"bec", "bec-mf"}:
        return v7.hangup(sess)
    if pack.name == "grayline":
        return "already on grayline\r\n"
    text = pop_to_previous(sess)
    if sess.host == "grayline":
        return return_to_pad(sess.user or "guest")
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


def _finger_tty(person: Session) -> str:
    if person.host in {"", "grayline"}:
        return "pad"
    if person.host == "tymnet":
        return "tym"
    if person.host in {"bec", "bec-mf"}:
        return "bec"
    return (person.host or "pad")[:8]


def _finger_where(person: Session) -> str:
    if person.host in {"", "grayline"}:
        return "PAD"
    if person.host == "terminal-addiction":
        return "TA"
    return person.host.upper()


def _finger_idle_seconds(person: Session) -> int:
    return max(0, int(time.monotonic() - person.last_active))


def _finger_idle_short(seconds: int) -> str:
    if seconds < 60:
        return ""
    minutes = seconds // 60
    if minutes < 60:
        return str(minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}:{minutes:02d}"
    return f"{hours // 24}d"


def _finger_idle_long(seconds: int) -> str:
    if seconds < 60:
        return ""
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}:{minutes:02d}"
    days, hours = divmod(hours, 24)
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} {hours}:{minutes:02d}"


def _finger_when(stamp: float) -> str:
    if stamp <= 0:
        return "*"
    return datetime.fromtimestamp(stamp).strftime("%a %b %d %H:%M")


def _finger_last_login(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "*"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return moment.astimezone().strftime("%a %b %d %H:%M")


def _finger_short(rows: list[Session]) -> str:
    lines = ["Login            Name             TTY      Idle  Where"]
    for person in rows:
        name = person.user or "guest"
        shown = "GUEST" if name.lower() == "guest" else name
        idle = _finger_idle_short(_finger_idle_seconds(person))
        lines.append(
            f"{shown:<16}{shown:<16}{_finger_tty(person):<8} {idle:>5}  {_finger_where(person)}"
        )
    return "\r\n".join(lines) + "\r\n"


def _finger_long(name: str, rows: list[Session], account) -> str:
    shown = "GUEST" if name.lower() == "guest" else name
    lines = [f"Login: {shown:<16} Name: {shown}"]
    lines.append(f"Directory: (none)               Shell: GL>")
    if rows:
        for person in rows:
            since = _finger_when(person.login_at)
            idle_sec = _finger_idle_seconds(person)
            idle = _finger_idle_long(idle_sec)
            tty = _finger_tty(person)
            where = _finger_where(person)
            if idle:
                lines.append(f"On since {since} on {tty} ({where}), idle {idle}")
            else:
                lines.append(f"On since {since} on {tty} ({where})")
    else:
        lines.append("Not logged in.")
        if account is not None:
            lines.append(f"Last login {_finger_last_login(account.last_login)}")
    mail = email_of(name) if account is not None else ""
    if mail:
        lines.append(f"Mail: {mail}")
    else:
        lines.append("No Mail.")
    plan = note_of(name) if account is not None else ""
    if plan.strip():
        lines.append("Plan:")
        for plan_line in plan.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            lines.append(plan_line)
    else:
        lines.append("No Plan.")
    return "\r\n".join(lines) + "\r\n"


def _finger_host(name: str) -> str | None:
    if name in {"bec", "big-evil"}:
        return "CIRCUIT  BEC\r\nBAUD     1200\r\nSTATE    UP\r\n"
    if name in {"tymnet"}:
        return "CIRCUIT  TYMNET\r\nBAUD     T1\r\nSTATE    UP\r\n"
    if name in {"ta", "terminal-addiction"}:
        return "CIRCUIT  TA\r\nBAUD     2400\r\nSTATE    OFFLINE\r\n"
    return None


def cmd_finger(sess: Session, args: list[str]) -> str:
    if sess.host == "grayline" and not site.verb_enabled("finger"):
        return "finger: not found\r\n"
    if not args:
        rows = sorted(live_sessions(), key=lambda other: (other.user or "", other.sid))
        if not rows:
            return "No one logged in.\r\n"
        return _finger_short(rows)
    name = args[0].lower()
    host_blurb = _finger_host(name)
    if host_blurb is not None:
        return host_blurb
    if name in {"sysop", "admin"}:
        return (
            f"Login: {name:<16} Name: {name}\r\n"
            "Directory: (none)               Shell: GL>\r\n"
            "Not logged in.\r\n"
            "Mail: not accepting mail\r\n"
            "No Plan.\r\n"
        )
    if name != "guest" and not valid_name(name):
        return f"finger: {name}: no such user.\r\n"
    account = get_account(name) if name != "guest" else None
    found = [other for other in live_sessions() if (other.user or "").lower() == name]
    if account is None and name != "guest" and not found:
        return f"finger: {name}: no such user.\r\n"
    if name == "guest" and not found:
        return "finger: guest: no such user.\r\n"
    return _finger_long(name, found, account)


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
        return return_to_pad(sess.user or "guest")
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
    ps1 = resolve_wallet(handle) or wallet_of(handle) or "none"
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
        head, _, rest = text.partition(" ")
        head = head.lower()
        if head in {"1", "email"} and rest.strip():
            return _save_email(sess, rest.strip())
        if head in {"3", "ps1"} and rest.strip():
            return _link_ps1(sess, rest.strip()) + _profile_menu(sess)
        if choice in {"1", "email"}:
            sess.phase = "profile_email"
            return ""
        if choice in {"2", "password"}:
            sess.phase = "profile_password"
            return ""
        if choice in {"3", "ps1"}:
            sess.phase = "profile_ps1"
            return ""
        if _EMAIL.fullmatch(text):
            return _save_email(sess, text)
        if chainrpc.normalize_ps1(text) or chainrpc.valid_shortname(text):
            return _link_ps1(sess, text) + _profile_menu(sess)
        return "not saved\r\n1 EMAIL\r\n2 PASSWORD\r\n3 PS1\r\nQ BACK\r\n"
    if phase == "profile_email":
        if not text:
            sess.phase = "profile"
            return _profile_menu(sess)
        return _save_email(sess, text)
    if phase == "profile_password":
        if not text:
            sess.phase = "profile"
            return _profile_menu(sess)
        if len(text) < 4:
            return "password: at least 4 characters\r\n"
        sess.pending_password = text
        sess.phase = "profile_password2"
        return ""
    if phase == "profile_password2":
        if not text:
            sess.pending_password = ""
            sess.phase = "profile"
            return _profile_menu(sess)
        if text != sess.pending_password:
            sess.pending_password = ""
            sess.phase = "profile_password"
            return "passwords differ\r\n"
        set_account_fields(sess.user or "", password_hash=hash_password(text), must_change=0)
        sess.pending_password = ""
        sess.phase = "profile"
        return "password saved\r\n" + _profile_menu(sess)
    if phase == "profile_ps1":
        if not text:
            sess.phase = "profile"
            return _profile_menu(sess)
        return _link_ps1(sess, text) + _profile_menu(sess)
    sess.phase = "shell"
    return ""


def _save_email(sess: Session, email: str) -> str:
    handle = sess.user or ""
    if not _EMAIL.fullmatch(email) or len(email) > 254:
        return "email: name@host\r\n"
    set_account_fields(handle, email=email)
    stored = email_of(handle)
    sess.phase = "profile"
    if stored != email:
        return "email not saved\r\n" + _profile_menu(sess)
    return "email saved\r\n" + _profile_menu(sess)


def _remember_wallet(sess: Session, value: str) -> str:
    """Store a ps1, or a short name when no node can resolve it yet."""
    handle = sess.user or ""
    named = not chainrpc.normalize_ps1(value)
    owner = wallet_owner(value)
    if owner and owner != handle:
        sess.phase = "profile"
        return "ps1 already linked\r\n"
    try:
        set_account_fields(handle, wallet=value)
    except sqlite3.IntegrityError:
        sess.phase = "profile"
        return "ps1 already linked\r\n"
    sess.phase = "profile"
    if named:
        return "name saved\r\n"
    return "ps1 saved\r\n"


def resolve_wallet(handle: str) -> str:
    """Turn a stored short name into its ps1 when a node answers."""
    current = wallet_of(handle)
    if not current or chainrpc.normalize_ps1(current):
        return current
    try:
        found = chainrpc.lookup_name(current)
    except (chainrpc.MethodMissing, chainrpc.RpcDown):
        return current
    if not found:
        return current
    owner = wallet_owner(found)
    if owner and owner != handle:
        return current
    try:
        set_account_fields(handle, wallet=found)
    except sqlite3.IntegrityError:
        return current
    return found


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
            if chainrpc.valid_shortname(text):
                return _remember_wallet(sess, text.strip())
            sess.phase = "profile"
            return "not saved: node did not answer\r\n"
    if not ps1:
        sess.phase = "profile"
        return "ps1 not found\r\n"
    owner = wallet_owner(ps1)
    if owner and owner != handle:
        sess.phase = "profile"
        return "ps1 already linked\r\n"
    return _remember_wallet(sess, ps1)


def perform_claim(sess: Session, flag_id: str) -> str:
    """Claim one flag for this account's ps1. The doorway answer stays here."""
    if not sess.user or sess.user == "guest" or get_account(sess.user) is None:
        return "logon required\r\n"
    flag_id = flag_id.strip()
    if not flag_id:
        return "claim: usage: claim FLAG_ID\r\n"
    ps1 = resolve_wallet(sess.user) or wallet_of(sess.user)
    if not ps1:
        return "link a wallet first: ps1 link can be found in profile_config\r\n"
    if not chainrpc.normalize_ps1(ps1):
        return "node did not answer\r\n"
    if claim_recorded(sess.user, flag_id):
        return "already yours\r\n"
    line, txid = chainrpc.claim_flag(flag_id, ps1)
    if line == "claimed" and txid:
        record_claim(sess.user, flag_id, ps1, txid)
        return "claimed\r\n"
    if line == "claimed":
        return "claim refused\r\n"
    return line if line.endswith("\r\n") else line + "\r\n"


def cmd_claim(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "claim: not found\r\n"
    if not args:
        return "claim: usage: claim FLAG_ID\r\n"
    return perform_claim(sess, args[0])


def cmd_pschain(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "pschain: not found\r\n"
    return chainrpc.pschain_text()


def _login_stamp(value: object) -> str:
    text = str(value or "").strip()
    return text or "none"


def cmd_leaders(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "leaders: not found\r\n"
    ranks = claim_ranks()
    ordered = sorted(ranks.items(), key=lambda item: (item[1][0], item[0]))
    if not ordered:
        return "no claims yet\r\n"
    lines = ["RANK  HANDLE        CLAIMS  LAST"]
    logins = {row["handle"]: row.get("last_login") for row in list_accounts()}
    for handle, (place, count) in ordered[:20]:
        lines.append(f"{place:<6}{handle:<14}{count:<8}{_login_stamp(logins.get(handle))}")
    return "\r\n".join(lines) + "\r\n"


def cmd_user_list(sess: Session, args: list[str]) -> str:
    if sess.host != "grayline":
        return "user_list: not found\r\n"
    rows = list_accounts()
    if not rows:
        return "no accounts\r\n"
    ranks = claim_ranks()
    lines = ["HANDLE        RANK  LAST"]
    for row in rows:
        handle = str(row["handle"])
        found = ranks.get(handle)
        place = "-" if found is None else str(found[0])
        lines.append(f"{handle:<14}{place:<6}{_login_stamp(row.get('last_login'))}")
    return "\r\n".join(lines) + "\r\n"


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
    sess.login_at = 0.0
    _clear_mail_draft(sess)
    _clear_group_draft(sess)
    sess.group_name = ""
    sess.group_art = 0
    return banner() + login_prompt()


COMMANDS: dict[str, Callable[[Session, list[str]], str]] = {
    "help": cmd_help,
    "?": cmd_help,
    "who": cmd_who,
    "motd": cmd_motd,
    "news": cmd_news,
    "mail": cmd_mail,
    "groups": cmd_groups,
    "wall": cmd_wall,
    "date": cmd_date,
    "clear": cmd_clear,
    "clr": cmd_clear,
    "full": cmd_full,
    "status": cmd_status,
    "map": cmd_map,
    "finger": cmd_finger,
    "whois": cmd_finger,
    "hosts": cmd_hosts,
    "host": cmd_hosts,
    "ls": cmd_ls,
    "dir": cmd_ls,
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
    "pschain": cmd_pschain,
    "leaders": cmd_leaders,
    "user_list": cmd_user_list,
}


def submit(sess: Session) -> str:
    text = sess.line
    sess.line = ""
    sess.hist_i = None
    sess.draft = ""
    if sess.user is None:
        return login_line(sess, text)
    if sess.host == "grayline" and sess.phase.startswith("mail"):
        body = mail_line(sess, text)
        return body + prompt_for(sess)
    if sess.host == "grayline" and sess.phase.startswith("group"):
        body = group_line(sess, text)
        return body + prompt_for(sess)
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
        return prompt_for(sess)
    if not sess.history or sess.history[-1] != stripped:
        sess.history.append(stripped)
    if len(sess.history) > MAX_HISTORY:
        del sess.history[: len(sess.history) - MAX_HISTORY]
    parts = stripped.split()
    cmd, args = parts[0].lower(), parts[1:]
    pack = get_pack(sess.host)
    if cmd == sess.host:
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
