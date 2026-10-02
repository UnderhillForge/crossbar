"""Banner, motd, lobby listing, and the host table text.

Greyline (GL>) is a public data network in no particular year: real clock,
carrier language, no 1985 and no 1993. A door's period lives on the pack
(`era`), and only after CONNECT. Do not put a door's dates on this banner.

Terminal chrome under data/text/:
  welcome.asc       public logon and return-to-pad
  menu_header.asc   header above help / ?
  main_menu.asc     editable command list for help / ?
  prompt.asc        grayline pad prompt; [time] [user]@[host]/[path]>
  news.asc          local NEWS bulletin (NNTP later)
  motd.asc          message of the day
  wall.asc          WALL posts (append via WALL <text>; edit over SSH)
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
# name -> (mtime_ns, crlf text). Reload when the file changes on disk.
_ASC_CACHE: dict[str, tuple[int, str]] = {}


def load_asc(name: str) -> str:
    """Load data/text/<name>.asc as CRLF terminal text. Rereads on mtime change."""
    path = _TEXT_DIR / f"{name}.asc"
    mtime_ns = path.stat().st_mtime_ns
    cached = _ASC_CACHE.get(name)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    raw = path.read_text(encoding="utf-8")
    text = raw.replace("\r\n", "\n").replace("\n", "\r\n")
    if not text.endswith("\r\n"):
        text += "\r\n"
    _ASC_CACHE[name] = (mtime_ns, text)
    return text


def logon_paint() -> str:
    """First paint from data/text/welcome.asc. No door year."""
    text = load_asc("welcome")
    # Blank line before LOGON: / pad_card.
    return text if text.endswith("\r\n\r\n") else text + "\r\n"


def menu_header() -> str:
    """Block header above the pad command list."""
    return load_asc("menu_header")


def pad_path_for(phase: str) -> str:
    """Subsection token for [path] in prompt.asc."""
    if phase.startswith("profile"):
        return "profile"
    if phase.startswith("mail"):
        return "mail"
    return "main"


def render_pad_prompt(user: str, path: str = "main", when: datetime | None = None) -> str:
    """Fill data/text/prompt.asc. Trailing space from the file is kept."""
    clock = (when or datetime.now()).strftime("%H:%M:%S")
    handle = user or "guest"
    text = load_asc("prompt").rstrip("\r\n")
    return (
        text.replace("[time]", clock)
        .replace("[user]", handle)
        .replace("[host]", SYSTEM_NAME)
        .replace("[path]", path)
    )


def banner() -> str:
    return logon_paint()


def login_prompt() -> str:
    return "LOGON: "


def header_line(handle: str) -> str:
    who = "GUEST" if handle.lower() == "guest" else handle
    text = f"GREYLINE PDN  •  T1  •  GL-01  •  {who}"
    return f"\x1b[7m{text}\x1b[0m\r\n"


def boot_steps() -> list[tuple[str, float, str]]:
    """One frame. A key is not required; there is no demo to skip."""
    return [(logon_paint(), 0.0, "banner")]


def boot_duration() -> float:
    return sum(delay for _text, delay, _kind in boot_steps())


def pad_card(handle: str) -> str:
    """Circuit open. Guest is GUEST; a handle is the stored name."""
    who = "GUEST" if handle.lower() == "guest" else handle
    ident = "GUEST ACCEPTED\r\n" if who == "GUEST" else f"IDENTITY  {who}\r\n"
    return header_line(handle) + ident + "CIRCUIT OPEN\r\n"


def return_to_pad(handle: str) -> str:
    """Welcome mark plus circuit card when a hop drops back on grayline."""
    return banner() + pad_card(handle)


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
        "  └─ TYMNET    T1    UP\r\n"
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
    "wall",
    "full",
    "status",
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


def motd_text() -> str:
    """Message of the day from data/text/motd.asc."""
    from crossbar import site

    custom = site.motd_override()
    if custom:
        return custom if custom.endswith("\r\n") else custom + "\r\n"
    return load_asc("motd")


def news_text() -> str:
    """Local bulletin from data/text/news.asc. NNTP is not attached yet."""
    from crossbar import site

    custom = site.news_override()
    if custom:
        return custom if custom.endswith("\r\n") else custom + "\r\n"
    return load_asc("news")


def help_text() -> str:
    from crossbar import site

    custom = site.help_override()
    if custom:
        body = custom if custom.endswith("\r\n") else custom + "\r\n"
    else:
        body = load_asc("main_menu")
    return menu_header() + "\r\n" + body


def ls_text() -> str:
    return "motd\r\nnews\r\nhelp\r\n"



