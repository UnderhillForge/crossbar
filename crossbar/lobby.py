"""Banner, motd, lobby listing, and the host table text.

Greyline (GL>) is a public data network in no particular year: real clock,
carrier language, no 1985 and no 1993. A door's period lives on the pack
(`era`), and only after CONNECT. Do not put a door's dates on this banner.

Terminal chrome under data/text/:
  welcome.asc/.ans  public logon and return-to-pad
  menu_header.asc/.ans  header above help / ?
  main_menu.asc/.ans    editable command list for help / ?
  prompt.asc/.ans       grayline pad prompt; [time] [user]@[host]/[path]>
  news.asc/.ans         local NEWS bulletin (NNTP later)
  motd.asc/.ans         message of the day
  wall.asc              WALL posts (append via WALL <text>; edit over SSH)

When ANSI is on and a matching .ans exists, that file is used; otherwise .asc.
.ans is often CP437; SAUCE footers are stripped. Drop .ans beside .asc for color.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from crossbar.config import SYSTEM_NAME
from crossbar.packs import get_pack

MOTD_LINE = "Orientation circuit is BEC."

# Grayline's directory. connect still does not open a TCP connection.
HOSTS = get_pack("grayline").hosts

LOGIN_LINE = "LOGON:"

_TEXT_DIR = Path(__file__).resolve().parent.parent / "data" / "text"
# cache_key -> (mtime_ns, crlf text). Reload when the file changes on disk.
_ASC_CACHE: dict[str, tuple[int, str]] = {}


def _strip_sauce(data: bytes) -> bytes:
    """Remove a trailing SAUCE record (and optional EOF byte) from ANSI art."""
    if len(data) >= 128 and data[-128:-122] == b"SAUCE00":
        data = data[:-128]
        if data.endswith(b"\x1a"):
            data = data[:-1]
    elif data.endswith(b"\x1a"):
        data = data[:-1]
    return data


def _decode_chrome(data: bytes, *, ans: bool) -> str:
    data = _strip_sauce(data)
    order = ("cp437", "utf-8", "latin-1") if ans else ("utf-8", "cp437", "latin-1")
    for enc in order:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _load_path(cache_key: str, path: Path, *, ans: bool) -> str:
    mtime_ns = path.stat().st_mtime_ns
    cached = _ASC_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    raw = _decode_chrome(path.read_bytes(), ans=ans)
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if not text.endswith("\r\n"):
        text += "\r\n"
    _ASC_CACHE[cache_key] = (mtime_ns, text)
    return text


def load_chrome(name: str, *, ansi: bool = False) -> str:
    """Load data/text/<name>.ans when ansi and present, else <name>.asc."""
    if ansi:
        ans_path = _TEXT_DIR / f"{name}.ans"
        if ans_path.is_file():
            return _load_path(f"{name}.ans", ans_path, ans=True)
    asc_path = _TEXT_DIR / f"{name}.asc"
    return _load_path(f"{name}.asc", asc_path, ans=False)


def load_asc(name: str) -> str:
    """Load data/text/<name>.asc as CRLF terminal text (monochrome path)."""
    return load_chrome(name, ansi=False)


def logon_paint(*, ansi: bool = False) -> str:
    """First paint from welcome.ans/.asc. No door year."""
    text = load_chrome("welcome", ansi=ansi)
    # Blank line before LOGON: / pad_card.
    return text if text.endswith("\r\n\r\n") else text + "\r\n"


def menu_header(*, ansi: bool = False) -> str:
    """Block header above the pad command list."""
    return load_chrome("menu_header", ansi=ansi)


def pad_path_for(phase: str) -> str:
    """Subsection token for [path] in prompt.asc."""
    if phase.startswith("profile"):
        return "profile"
    if phase.startswith("mail"):
        return "mail"
    if phase.startswith("group"):
        return "groups"
    return "main"


def render_pad_prompt(
    user: str,
    path: str = "main",
    when: datetime | None = None,
    *,
    ansi: bool = False,
) -> str:
    """Fill prompt.ans/.asc. Trailing space from the file is kept."""
    clock = (when or datetime.now()).strftime("%H:%M:%S")
    handle = user or "guest"
    text = load_chrome("prompt", ansi=ansi).rstrip("\r\n")
    return (
        text.replace("[time]", clock)
        .replace("[user]", handle)
        .replace("[host]", SYSTEM_NAME)
        .replace("[path]", path)
    )


def banner(*, ansi: bool = False) -> str:
    return logon_paint(ansi=ansi)


def login_prompt() -> str:
    return "LOGON: "


def header_line(handle: str) -> str:
    who = "GUEST" if handle.lower() == "guest" else handle
    text = f"GREYLINE PDN  •  T1  •  GL-01  •  {who}"
    return f"\x1b[7m{text}\x1b[0m\r\n"


def boot_steps(*, ansi: bool = True) -> list[tuple[str, float, str]]:
    """One frame. A key is not required; there is no demo to skip."""
    return [(logon_paint(ansi=ansi), 0.0, "banner")]


def boot_duration() -> float:
    return sum(delay for _text, delay, _kind in boot_steps())


def pad_card(handle: str) -> str:
    """Circuit open. Guest is GUEST; a handle is the stored name."""
    who = "GUEST" if handle.lower() == "guest" else handle
    ident = "GUEST ACCEPTED\r\n" if who == "GUEST" else f"IDENTITY  {who}\r\n"
    return header_line(handle) + ident + "CIRCUIT OPEN\r\n"


def return_to_pad(handle: str, *, ansi: bool = False) -> str:
    """Welcome mark plus circuit card when a hop drops back on grayline."""
    return banner(ansi=ansi) + pad_card(handle)


def pad_hosts_text() -> str:
    from crossbar import site

    lines = ["NAME     TITLE                              BAUD   STATE"]
    for label, title, baud, state in site.pad_host_rows():
        lines.append(f"{label:<9}{title:<39}{baud:<7}{state}")
    return "\r\n".join(lines) + "\r\n"


def pad_map_text() -> str:
    return (
        "GL-01\r\n"
        "  ├─ BEC       1200  UP\r\n"
        "  ├─ TA        2400  OFFLINE\r\n"
        "  ├─ TYMNET    T1    UP\r\n"
        "  └─ MUDPROTO  9600  UP\r\n"
    )


# Tab candidates at GL>. Canonical spellings. No UNIX tree, no orientation host.
PAD_WORDS = (
    "?",
    "help",
    "hosts",
    "finger",
    "who",
    "connect",
    "date",
    "clear",
    "clr",
    "motd",
    "news",
    "mail",
    "groups",
    "wall",
    "full",
    "status",
    "ansi",
    "bye",
    "logout",
    "map",
    "profile_config",
    "claim",
    "pschain",
    "leaders",
    "user_list",
    "bec",
    "tymnet",
    "mudproto",
    "ta",
    "terminal-addiction",
)


def _columns(names: list[str], width: int = 72) -> str:
    gap = 2
    widest = max(len(name) for name in names)
    cols = max(1, (width + gap) // (widest + gap))
    rows = (len(names) + cols - 1) // cols
    lines = []
    for row in range(rows):
        parts = []
        for col in range(cols):
            index = col * rows + row
            if index < len(names):
                parts.append(names[index].ljust(widest))
        lines.append((" " * gap).join(parts).rstrip())
    return "\r\n".join(lines)


def pad_tab(line: str, prompt: str = "") -> tuple[str, str]:
    """Complete one token at the pad prompt. Returns (echo, new line). No match is a bell."""
    if line.endswith(" ") or not line:
        base, token = line, ""
    elif " " in line:
        base, token = line.rsplit(" ", 1)
        base += " "
    else:
        base, token = "", line
    pref = token.lower()
    matches = [word for word in PAD_WORDS if word.lower().startswith(pref)]
    if not matches:
        return "\a", line
    if len(matches) == 1:
        canon = matches[0]
        erase = "\b \b" * len(token)
        return erase + canon + " ", base + canon + " "
    listing = _columns(matches)
    shown = prompt or render_pad_prompt("guest", "main")
    return f"\r\n{listing}\r\n{shown}{line}", line


def motd_text(*, ansi: bool = False) -> str:
    """Message of the day from motd.ans/.asc."""
    from crossbar import site

    custom = site.motd_override()
    if custom:
        return custom if custom.endswith("\r\n") else custom + "\r\n"
    return load_chrome("motd", ansi=ansi)


def news_text(*, ansi: bool = False) -> str:
    """Local bulletin from news.ans/.asc. NNTP is not attached yet."""
    from crossbar import site

    custom = site.news_override()
    if custom:
        return custom if custom.endswith("\r\n") else custom + "\r\n"
    return load_chrome("news", ansi=ansi)


def help_text(*, ansi: bool = False) -> str:
    from crossbar import site

    custom = site.help_override()
    if custom:
        body = custom if custom.endswith("\r\n") else custom + "\r\n"
    else:
        body = load_chrome("main_menu", ansi=ansi)
    return menu_header(ansi=ansi) + "\r\n" + body


def ls_text() -> str:
    return "motd\r\nnews\r\nhelp\r\n"



