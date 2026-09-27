"""UW PICO 1.0 as a session mode. ANSI only. No curses, no real tty."""

from __future__ import annotations

from crossbar.session import Session, prompt_for

_COLS = 80
_BODY = 20
_HELP1 = "^G Get Help  ^O WriteOut  ^R Read File"
_HELP2 = "^X Exit  ^J Justify  ^W Where is"
_HELP_BODY = (
    "UW PICO 1.0  —  help\n"
    "\n"
    "Arrows move. Backspace deletes. Enter splits a line.\n"
    "^O writes the buffer if this uid may write the path.\n"
    "^X leaves. If the buffer changed, Y writes, N discards, C stays.\n"
    "^W searches. ^G returns here. ^J and ^R are not on this binary.\n"
    "\n"
    "This is not a shell. It does not run what you type."
)


def _pad(text: str) -> str:
    return text[:_COLS].ljust(_COLS)


def full_paint(sess: Session, status: str = "") -> str:
    lines = sess.pico_lines or [""]
    name = sess.pico_path or "New buffer"
    header = _pad(f" UW PICO 1.0    File: {name}")
    rows = ["\x1b[2J\x1b[H\x1b[7m" + header + "\x1b[0m"]
    top = sess.pico_top
    for offset in range(_BODY):
        index = top + offset
        text = lines[index] if index < len(lines) else ""
        rows.append(text[:_COLS])
    rows.append("\x1b[7m" + _pad(_HELP1) + "\x1b[0m")
    rows.append("\x1b[7m" + _pad(_HELP2) + "\x1b[0m")
    if status:
        rows.append(status[:_COLS])
    return "\r\n".join(rows)


def _touch_line(sess: Session) -> str:
    screen = 2 + (sess.pico_row - sess.pico_top)
    text = ""
    if 0 <= sess.pico_row < len(sess.pico_lines):
        text = sess.pico_lines[sess.pico_row][:_COLS]
    return f"\x1b[{screen};1H\x1b[K{text}"


def _status(text: str) -> str:
    return f"\x1b[24;1H\x1b[K{text[:_COLS]}"


def _clamp(sess: Session) -> None:
    if not sess.pico_lines:
        sess.pico_lines = [""]
    sess.pico_row = max(0, min(sess.pico_row, len(sess.pico_lines) - 1))
    sess.pico_col = max(0, min(sess.pico_col, len(sess.pico_lines[sess.pico_row])))
    if sess.pico_row < sess.pico_top:
        sess.pico_top = sess.pico_row
    if sess.pico_row >= sess.pico_top + _BODY:
        sess.pico_top = sess.pico_row - _BODY + 1


def _write(sess: Session) -> str:
    if sess.pico_ro:
        return _status("Read only")
    if not sess.pico_path:
        return _status("No file name")
    from crossbar.v7 import write_virtual

    err = write_virtual(sess, sess.pico_path, "\n".join(sess.pico_lines) + "\n")
    if err:
        return _status(err)
    sess.pico_dirty = False
    return _status(f"Wrote {len(sess.pico_lines)} lines")


def _exit_editor(sess: Session) -> str:
    sess.v7_phase = "shell"
    sess.pico_ask = ""
    return "\x1b[2J\x1b[H" + prompt_for(sess)


def _insert(sess: Session, ch: str) -> str:
    if sess.pico_ro:
        return _status("Read only")
    row = sess.pico_lines[sess.pico_row]
    sess.pico_lines[sess.pico_row] = row[: sess.pico_col] + ch + row[sess.pico_col :]
    sess.pico_col += 1
    sess.pico_dirty = True
    return _touch_line(sess)


def _backspace(sess: Session) -> str:
    if sess.pico_ro:
        return _status("Read only")
    row = sess.pico_lines[sess.pico_row]
    if sess.pico_col > 0:
        sess.pico_lines[sess.pico_row] = row[: sess.pico_col - 1] + row[sess.pico_col :]
        sess.pico_col -= 1
        sess.pico_dirty = True
        return _touch_line(sess)
    if sess.pico_row == 0:
        return ""
    prev = sess.pico_lines[sess.pico_row - 1]
    sess.pico_col = len(prev)
    sess.pico_lines[sess.pico_row - 1] = prev + row
    del sess.pico_lines[sess.pico_row]
    sess.pico_row -= 1
    sess.pico_dirty = True
    _clamp(sess)
    return full_paint(sess)


def _newline(sess: Session) -> str:
    if sess.pico_ro:
        return _status("Read only")
    row = sess.pico_lines[sess.pico_row]
    sess.pico_lines[sess.pico_row] = row[: sess.pico_col]
    sess.pico_lines.insert(sess.pico_row + 1, row[sess.pico_col :])
    sess.pico_row += 1
    sess.pico_col = 0
    sess.pico_dirty = True
    _clamp(sess)
    return full_paint(sess)


def _search(sess: Session) -> str:
    needle = sess.pico_search
    start = sess.pico_row
    for offset in range(len(sess.pico_lines)):
        index = (start + offset) % len(sess.pico_lines)
        if needle and needle in sess.pico_lines[index]:
            sess.pico_row = index
            sess.pico_col = sess.pico_lines[index].index(needle)
            _clamp(sess)
            return full_paint(sess, f"Found: {needle}")
    return _status("Not found")


def _ask(sess: Session, ch: str) -> str:
    if sess.pico_ask == "search":
        if ch == "\r":
            sess.pico_ask = ""
            return _search(sess)
        if ch in {"\x7f", "\x08"}:
            sess.pico_search = sess.pico_search[:-1]
        elif ch >= " " and ch <= "~":
            sess.pico_search += ch
        return _status("Search: " + sess.pico_search)
    letter = ch.lower()
    if letter == "y":
        note = _write(sess)
        if "permission" in note or "Read only" in note or "No file" in note:
            sess.pico_ask = ""
            return note
        return _exit_editor(sess)
    if letter == "n":
        sess.pico_dirty = False
        return _exit_editor(sess)
    if letter in {"c", "\x03", "\x07"}:
        sess.pico_ask = ""
        return _status("")
    return _status("Save modified buffer? Y/N/Cancel")


def _command(sess: Session, ch: str) -> str:
    if ch == "\x07":
        sess.pico_lines = _HELP_BODY.split("\n")
        sess.pico_row = 0
        sess.pico_col = 0
        sess.pico_top = 0
        sess.pico_ro = True
        sess.pico_dirty = False
        return full_paint(sess, "Help. ^X returns to the shell.")
    if ch == "\x0f":
        return _write(sess)
    if ch == "\x18":
        if sess.pico_dirty:
            sess.pico_ask = "save"
            return _status("Save modified buffer? Y/N/Cancel")
        return _exit_editor(sess)
    if ch == "\x17":
        sess.pico_ask = "search"
        sess.pico_search = ""
        return _status("Search: ")
    if ch in {"\x0a", "\x12"}:
        return _status("Not implemented")
    return ""


def feed(sess: Session, data: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(data):
        ch = data[index]
        if ch == "\x1b" and index + 2 < len(data) and data[index + 1] == "[":
            arrow = data[index + 2]
            index += 3
            if arrow == "A":
                sess.pico_row -= 1
            elif arrow == "B":
                sess.pico_row += 1
            elif arrow == "C":
                sess.pico_col += 1
            elif arrow == "D" and sess.pico_col:
                sess.pico_col -= 1
            _clamp(sess)
            out.append(_touch_line(sess))
            continue
        index += 1
        if sess.pico_ask:
            out.append(_ask(sess, ch))
            continue
        if ch in {"\x07", "\x0f", "\x18", "\x17", "\x0a", "\x12"}:
            out.append(_command(sess, ch))
            continue
        if ch in {"\x7f", "\x08"}:
            out.append(_backspace(sess))
            continue
        if ch == "\r":
            out.append(_newline(sess))
            continue
        if " " <= ch <= "~":
            out.append(_insert(sess, ch))
    return "".join(out)
