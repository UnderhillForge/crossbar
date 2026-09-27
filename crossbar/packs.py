"""In-process packs. Connecting changes the session host. No sockets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HostRef:
    name: str
    up: bool
    baud: int | None = None


@dataclass(frozen=True)
class Pack:
    name: str
    up: bool
    commands: frozenset[str]
    hosts: tuple[HostRef, ...]
    banner: str
    here_line: str
    baud_max: int
    # Directory under packs/ and which interpreter reads it. Empty means not a UNIX pack.
    flavor: str = ""
    data: str = ""
    # Door period, e.g. "1985-1993". Empty means this host is not a dated door.
    era: str = ""


_GRAYLINE_COMMANDS = frozenset(
    {
        "help",
        "?",
        "who",
        "whois",
        "finger",
        "hosts",
        "host",
        "connect",
        "date",
        "motd",
        "news",
        "full",
        "status",
        "bye",
        "map",
        "login",
        "logout",
        "exit",
        "verify",
        "profile_config",
        "claim",
        "chain",
        "pschain",
        "leaders",
        "user_list",
    }
)

_TYMNET_COMMANDS = frozenset(
    {"help", "hosts", "connect", "bye", "g", "finger", "who"}
)

# The board stays on disk. It does not answer, and it no longer offers the BEC door.
_TA_COMMANDS = frozenset({"bye", "g"})


def _tymnet_banner() -> str:
    return (
        "TYMNET\r\n"
        "pad ready\r\n"
        "carrier detect ...... tymnet\r\n"
        "\r\n"
    )


def ta_menu() -> str:
    return (
        "TERMINAL ADDICTION\r\n"
        "Renegade stub\r\n"
        "\r\n"
        "[M] Message bases\r\n"
        "[F] File areas\r\n"
        "[D] Doors\r\n"
        "[H] Hosts\r\n"
        "[G] Goodbye\r\n"
        "[?] Help\r\n"
        "\r\n"
        "no messages yet.\r\n"
    )


PACKS: dict[str, Pack] = {
    "grayline": Pack(
        name="grayline",
        up=True,
        commands=_GRAYLINE_COMMANDS,
        hosts=(
            HostRef("bec", True, 1200),
            HostRef("tymnet", True, 9600),
            HostRef("terminal-addiction", False, 2400),
        ),
        banner="CONNECTED  T1\r\n",
        here_line="Orientation circuit: CONNECT BEC.",
        baud_max=1_544_000,
    ),
    "tymnet": Pack(
        name="tymnet",
        up=True,
        commands=_TYMNET_COMMANDS,
        hosts=(
            HostRef("tymnet", True),
            HostRef("grayline", True),
            HostRef("terminal-addiction", False, 2400),
            HostRef("office-314", False),
        ),
        banner=_tymnet_banner(),
        here_line="pad ready",
        baud_max=9600,
    ),
    "terminal-addiction": Pack(
        name="terminal-addiction",
        up=False,
        commands=_TA_COMMANDS,
        hosts=(
            HostRef("terminal-addiction", False, 2400),
            HostRef("grayline", True),
            HostRef("tymnet", True),
        ),
        banner=ta_menu(),
        here_line="no messages yet.",
        baud_max=2400,
        era="1994-bbs",
    ),
    "bec": Pack(
        name="bec",
        up=True,
        commands=frozenset(),
        hosts=(HostRef("bec", True, 1200), HostRef("bec-mf", True, 9600)),
        banner="",
        here_line="UNIX V7",
        baud_max=1200,
        flavor="v7",
        data="big-evil",
        era="1985-1993",
    ),
    "bec-mf": Pack(
        name="bec-mf",
        up=True,
        commands=frozenset(),
        hosts=(HostRef("bec-mf", True, 9600),),
        banner="",
        here_line="mainframe",
        baud_max=9600,
        flavor="v7",
        data="big-evil",
        era="1985-1993",
    ),
}


def get_pack(name: str) -> Pack:
    if name in {"big-evil", "bec"}:
        name = "bec"
    elif name in {"mainframe", "bec-mf"}:
        name = "bec-mf"
    return PACKS.get(name, PACKS["grayline"])


def hosts_listing(current: str) -> str:
    pack = get_pack(current)
    lines = [f"{'host':<22}status"]
    for host in pack.hosts:
        if host.name == current:
            status = "here"
        elif not host.up:
            status = "dark/offline"
        elif host.baud:
            status = str(host.baud)
        else:
            status = "up"
        lines.append(f"{host.name:<22}{status}")
    return "\r\n".join(lines) + "\r\n"
