"""Banner, motd, lobby listing, and the host table text.

Greyline (GL>) is a public data network in no particular year: real clock,
carrier language, no 1985 and no 1993. A door's period lives on the pack
(`era`), and only after CONNECT. Do not put a door's dates on this banner.
"""

from __future__ import annotations

from crossbar.packs import get_pack

MOTD_LINE = "Orientation circuit is BEC."

# Grayline's directory. connect still does not open a TCP connection.
HOSTS = get_pack("grayline").hosts


LOGIN_LINE = "LOGON:"


def logon_paint() -> str:
    """First paint. No year, no logo, no phosphor."""
    return (
        "CONNECTED  T1    DTE 03    NODE GL-01\r\n"
        "\r\n"
        "GREYLINE PUBLIC DATA NETWORK\r\n"
        "PAD READY\r\n"
        "\r\n"
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
    "motd",
    "news",
    "full",
    "status",
    "bye",
    "logout",
    "map",
    "profile_config",
    "claim",
    "chain",
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


def pad_tab(line: str) -> tuple[str, str]:
    """Complete one token at GL>. Returns (echo, new line). No match is a bell."""
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
    return f"\r\n{listing}\r\nGL> {line}", line


def motd_text() -> str:
    from crossbar import site

    custom = site.motd_override()
    if custom:
        return custom if custom.endswith("\r\n") else custom + "\r\n"
    return (
        "GREYLINE PDN\r\n"
        f"{MOTD_LINE}\r\n"
        "Terminal Addiction is offline.\r\n"
    )


def news_text() -> str:
    # Live NNTP belongs on the PAD, never in a door spool. Nothing is wired.
    return "NEWS UNAVAILABLE\r\n"


def help_text() -> str:
    from crossbar import site

    custom = site.help_override()
    if custom:
        return custom if custom.endswith("\r\n") else custom + "\r\n"
    return (
        "  HOSTS     circuits\r\n"
        "  CONNECT   <name>\r\n"
        "  STATUS    this pad\r\n"
        "  FULL      extended\r\n"
        "  WHO       stations\r\n"
        "  FINGER    <id>\r\n"
        "  NEWS      nntp\r\n"
        "  MOTD\r\n"
        "  DATE\r\n"
        "  BYE\r\n"
        "  PROFILE_CONFIG\r\n"
        "  CLAIM     <flag>\r\n"
        "  CHAIN\r\n"
        "  orientation circuit is BEC\r\n"
    )


def ls_text() -> str:
    return "motd\r\nnews\r\nhelp\r\n"



