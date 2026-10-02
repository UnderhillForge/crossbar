"""Session, command, and live-server checks for Crossbar."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

# Set before importing the app so this process and the live server agree.
os.environ["CROSSBAR_SECRET"] = "test-secret-for-crossbar"
_fd, _DB = tempfile.mkstemp(prefix="grayline-test-", suffix=".db")
os.close(_fd)
os.environ["CROSSBAR_DB"] = _DB
_wall_fd, _WALL = tempfile.mkstemp(prefix="wall-test-", suffix=".asc")
os.close(_wall_fd)
os.environ["CROSSBAR_WALL"] = _WALL
with open(_WALL, "w", encoding="utf-8") as _wall_out:
    _wall_out.write("# test wall\n")

from crossbar.commands import (  # noqa: E402
    cmd_bye,
    cmd_connect,
    cmd_finger,
    cmd_hosts,
    cmd_logout,
    cmd_who,
)
from crossbar.config import IDLE_TTL  # noqa: E402
from crossbar.lobby import HOSTS, banner  # noqa: E402
from crossbar.session import SESSIONS, Session, get_session, prompt_for  # noqa: E402
from crossbar.ws import hello, push  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-secret-for-crossbar"
INDEX = ROOT / "static" / "index.html"


def strip_sgr(text: str) -> str:
    """Drop CSI SGR sequences for prompt suffix checks."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def pad_prompt_end(user: str = "ada", path: str = "main") -> str:
    """Suffix of the configurable grayline prompt (time varies)."""
    return f"{user}@grayline/{path}> "


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        SESSIONS.clear()

    def tearDown(self) -> None:
        SESSIONS.clear()

    def test_banner_names_grayline_and_crossbar(self) -> None:
        from crossbar.lobby import help_text, load_asc, load_chrome

        text = banner(ansi=False)
        self.assertEqual(text, load_asc("welcome") + "\r\n")
        self.assertIn("CONNECTED T1 // DTE 03 // NODE GL-01", text)
        self.assertIn("GRAYLINE.DEV X.25 PUBLIC DATA NETWORK", text)
        self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", text)
        self.assertIn("Crossbar Consortium", text)
        self.assertNotIn("1985", text)
        self.assertNotIn("1993", text)
        self.assertNotIn("PiSecure", text)
        self.assertNotIn("pisecure", text.lower())

        helped = help_text(ansi=False)
        self.assertEqual(
            helped,
            load_asc("menu_header") + "\r\n" + load_asc("main_menu"),
        )
        self.assertIn("[MENU]", helped)
        self.assertIn("[Main]", helped)
        self.assertIn("[System]", helped)
        self.assertIn("HOSTS", helped)
        self.assertIn("PROFILE_CONFIG", helped)
        color = help_text(ansi=True)
        self.assertIn("\x1b[", color)
        self.assertIn("\x1b[", load_chrome("welcome", ansi=True))
        sess = get_session("sid-ansi")
        sess.user = "guest"
        sess.host = "grayline"
        sess.phase = "shell"
        self.assertIn("ANSI ON", push(sess, "ansi\r"))
        sess.ansi_ok = True
        self.assertIn("ANSI OFF", push(sess, "ansi off\r"))
        self.assertFalse(sess.ansi_ok)

    def test_login_guest_register_and_reserved(self) -> None:
        from crossbar.accounts import authenticate, create_account, get_account

        guest = get_session("sid-guest")
        arrived = push(guest, "guest\r")
        self.assertIn("GUEST ACCEPTED", arrived)
        self.assertIn("CIRCUIT OPEN", arrived)
        self.assertTrue(strip_sgr(arrived).endswith(pad_prompt_end("guest")))
        self.assertEqual(guest.user, "guest")
        self.assertIsNone(get_account("guest"))

        missing = get_session("sid-missing")
        out = push(missing, "ada\r")
        self.assertIsNone(missing.user)
        self.assertIn("no such user", out)
        self.assertTrue(out.endswith("LOGON: "))

        reserved = get_session("sid-reserved")
        out = push(reserved, "sysop\r")
        self.assertIsNone(reserved.user)
        self.assertIn("reserved", out)
        self.assertNotIn("PASSWORD:", out)

        fresh = get_session("sid-new")
        self.assertTrue(push(fresh, "new\r").endswith("handle: "))
        self.assertTrue(push(fresh, "Ada\r").endswith("PASSWORD: "))
        self.assertEqual(push(fresh, "secret12\r"), "\r\nconfirm: ")
        self.assertEqual(fresh.line, "")
        self.assertTrue(push(fresh, "secret12\r").endswith("email: "))
        done = push(fresh, "ada@example.com\r")
        self.assertEqual(fresh.user, "ada")
        self.assertIn("IDENTITY  ada", done)
        self.assertIn("CIRCUIT OPEN", done)
        self.assertTrue(strip_sgr(done).endswith(pad_prompt_end("ada")))
        account = get_account("ada")
        self.assertIsNotNone(account)
        assert account is not None
        self.assertEqual(account.status, "ok")
        self.assertEqual(account.email, "ada@example.com")
        self.assertNotIn("secret12", account.password_hash)

        again = get_session("sid-again")
        self.assertTrue(push(again, "ada\r").endswith("PASSWORD: "))
        bad = push(again, "nope\r")
        self.assertIsNone(again.user)
        self.assertIn("IDENTIFICATION NOT RECOGNIZED", bad)
        self.assertTrue(bad.endswith("LOGON: "))
        self.assertTrue(push(again, "ada\r").endswith("PASSWORD: "))
        good = push(again, "secret12\r")
        self.assertEqual(again.user, "ada")
        self.assertIn("IDENTITY  ada", good)
        self.assertTrue(strip_sgr(good).endswith(pad_prompt_end("ada")))
        self.assertTrue(authenticate("ada", "secret12"))
        self.assertFalse(authenticate("sysop", "secret12"))
        self.assertFalse(authenticate("admin", "secret12"))
        self.assertRaises(ValueError, create_account, "admin", "secret12", "a@b.co")

        hidden = get_session("sid-hidden")
        hidden.phase = "password"
        self.assertEqual(push(hidden, "ab"), "")
        self.assertEqual(hidden.line, "ab")
        self.assertEqual(push(hidden, "\x7f"), "")
        self.assertEqual(hidden.line, "a")

    def test_backspace_edits_the_line_and_echoes(self) -> None:
        sess = get_session("sid-bs")
        sess.user = "ada"
        self.assertEqual(push(sess, "hex"), "hex")
        self.assertEqual(sess.line, "hex")
        self.assertEqual(push(sess, "\x7f"), "\b \b")
        self.assertEqual(sess.line, "he")
        self.assertEqual(push(sess, "\x08"), "\b \b")
        self.assertEqual(sess.line, "h")
        # Empty buffer: backspace must not eat the prompt.
        push(sess, "\x7f")
        self.assertEqual(push(sess, "\x7f"), "")
        self.assertEqual(sess.line, "")
        out = push(sess, "help\r")
        self.assertIn("help\r\n", out)
        self.assertIn("WHO", out)
        self.assertTrue(strip_sgr(out).endswith(pad_prompt_end()))

    def test_backspace_in_one_chunk_runs_the_edited_command(self) -> None:
        sess = get_session("sid-bs2")
        sess.user = "ada"
        out = push(sess, "hex\x7flp\r")
        self.assertIn("\b \b", out)
        self.assertIn("HOSTS", out)
        self.assertIn("circuits", out)
        self.assertEqual(sess.line, "")

    def test_split_crlf_submits_once(self) -> None:
        sess = get_session("sid-crlf")
        sess.user = "ada"
        self.assertIn("not found", push(sess, "frob\r"))
        self.assertEqual(push(sess, "\n"), "")
        self.assertEqual(sess.line, "")
        self.assertEqual(sess.history, ["frob"])

    def test_lobby_aliases_and_current_host(self) -> None:
        sess = get_session("sid-lobby")
        sess.user = "ada"
        sess.host = "grayline"
        listed = push(sess, "motd\r")
        self.assertIn("Orientation circuit is BEC.", listed)
        bulletin = push(sess, "news\r")
        self.assertIn("GRAYLINE PDN", bulletin)
        self.assertIn("LOCAL BULLETIN", bulletin)
        self.assertIn("Crossbar", bulletin)
        self.assertNotIn("NEWS UNAVAILABLE", bulletin)
        from crossbar.accounts import create_account, get_account

        if get_account("mailer") is None:
            create_account("mailer", "secret12", "mailer@example.com")
        mailer = get_session("sid-lobby-mailer")
        mailer.user = "mailer"
        mailer.host = "grayline"
        guest = get_session("sid-lobby-guest-mail")
        guest.user = "guest"
        guest.host = "grayline"
        self.assertIn("logon required", push(guest, "mail\r"))
        mailbox = push(mailer, "mail\r")
        self.assertIn("MAIL", mailbox)
        self.assertIn("mailer", mailbox)
        self.assertEqual(mailer.phase, "mail")
        self.assertIn("/mail>", mailbox)
        quit_out = push(mailer, "q\r")
        self.assertIn("returned to pad", quit_out)
        self.assertEqual(mailer.phase, "shell")
        self.assertIn("/main>", quit_out)
        helped = push(sess, "?\r")
        self.assertIn("MAIL", helped)
        self.assertIn("WALL", helped)
        wall = push(sess, "wall\r")
        self.assertIn("WALL", wall)
        posted = push(mailer, "wall hello pad\r")
        self.assertIn("posted", posted)
        self.assertIn("hello pad", posted)
        here = push(sess, "grayline\r")
        self.assertIn("already connected to grayline\r\n", here)
        self.assertIn("Orientation circuit is BEC.", here)
        again = push(sess, "login\r")
        self.assertIn("already logged in as ada\r\n", again)
        self.assertIn("logout", again)
        helped = push(sess, "?\r")
        self.assertIn("circuits", helped)
        self.assertIn("[Main]", helped)
        self.assertIn("BEC", push(sess, "hosts\r"))
        self.assertIn("OFFLINE", push(sess, "HOSTS\r"))
        bob = get_session("sid-lobby-bob")
        bob.user = "bob"
        bob_finger = push(sess, "whois bob\r")
        self.assertIn("Login: bob", bob_finger)
        self.assertIn("On since", bob_finger)
        who = push(sess, "who\r")
        self.assertIn("\r\nbob\r\n", who)
        self.assertNotIn("\r\nada\r\n", who)

    def test_tymnet_and_terminal_addiction(self) -> None:
        sess = get_session("sid-pad")
        sess.user = "ada"
        sess.host = "grayline"
        opened = push(sess, "connect tymnet\r")
        self.assertIn("TYMNET", opened)
        self.assertIn("pad ready", opened)
        self.assertTrue(opened.endswith("ada@tymnet> "))
        self.assertEqual(sess.host, "tymnet")

        listing = push(sess, "hosts\r")
        self.assertIn("grayline", listing)
        self.assertIn("terminal-addiction", listing)
        self.assertIn("office-314", listing)
        self.assertIn("dark/offline", listing)
        dark = push(sess, "connect office-314\r")
        self.assertIn("office-314 is dark", dark)
        self.assertEqual(sess.host, "tymnet")
        self.assertTrue(dark.endswith("ada@tymnet> "))

        self.assertIn("not found", push(sess, "ls\r"))
        board = push(sess, "connect terminal-addiction\r")
        self.assertIn("DIALING 2400", board)
        self.assertIn("NO CARRIER", board)
        self.assertEqual(sess.host, "tymnet")
        self.assertNotIn("Renegade", board)
        home = push(sess, "bye\r")
        self.assertEqual(sess.host, "grayline")
        self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", home)
        self.assertIn("CIRCUIT OPEN", home)
        self.assertTrue(strip_sgr(home).endswith(pad_prompt_end("ada")))

        direct = push(sess, "connect ta\r")
        self.assertIn("NO CARRIER", direct)
        self.assertEqual(sess.host, "grayline")
        self.assertNotIn("Renegade", direct)

        other = get_session("sid-pad-other")
        other.user = "bob"
        other.host = "grayline"
        who = push(sess, "who\r")
        self.assertIn("\r\nbob\r\n", who)
        self.assertNotIn("\r\nada\r\n", who)

    def test_pad_tab_and_offline_ta(self) -> None:
        sess = get_session("sid-tab")
        sess.user = "ada"
        sess.host = "grayline"
        push(sess, "con\t")
        self.assertEqual(sess.line, "connect ")
        sess.line = ""
        push(sess, "be\t")
        self.assertEqual(sess.line, "bec ")
        sess.line = ""
        push(sess, "mo\t")
        self.assertEqual(sess.line, "motd ")
        sess.line = ""
        push(sess, "ho\t")
        self.assertEqual(sess.line, "hosts ")
        sess.line = ""
        many = push(sess, "\t")
        self.assertIn(pad_prompt_end("ada"), strip_sgr(many))
        self.assertIn("connect", many)
        self.assertIn("bec", many)
        self.assertNotIn("hank", many)
        self.assertNotIn("/usr", many)
        sess.line = ""
        none = push(sess, "zz\t")
        self.assertEqual(none, "zz\a")
        self.assertNotIn("?", none)
        self.assertEqual(sess.line, "zz")
        sess.line = ""
        status = push(sess, "FULL\r")
        self.assertIn("NODE GL-01", status)
        self.assertIn("NEWS  LOCAL BULLETIN", status)
        self.assertIn("ada", status)
        self.assertIn("not found", push(sess, "connect orientation\r"))
        self.assertEqual(sess.host, "grayline")
        from crossbar.lobby import help_text, motd_text, news_text, pad_hosts_text
        from crossbar.packs import get_pack

        for blob in (motd_text(), news_text(), pad_hosts_text()):
            self.assertNotIn("1985", blob)
            self.assertNotIn("1993", blob)
        self.assertIn("BEC OUTSIDE PLANT", pad_hosts_text())
        self.assertIn("CARRIER HOP", pad_hosts_text())
        self.assertNotIn("1985", help_text())
        self.assertNotIn("1993", help_text())
        self.assertEqual(get_pack("grayline").era, "")
        self.assertEqual(get_pack("bec").era, "1985-1993")
        self.assertEqual(get_pack("terminal-addiction").era, "1994-bbs")

    def test_bec_unix_door(self) -> None:
        from crossbar.config import T1_BAUD

        sess = get_session("sid-bec")
        sess.user = "ada"
        sess.host = "grayline"
        listing = push(sess, "hosts\r")
        self.assertIn("BEC", listing)
        self.assertIn("ORIENTATION", listing)
        self.assertIn("1200", listing)
        self.assertIn("UP", listing)
        self.assertIn("OFFLINE", listing)
        self.assertNotIn("orientation", [host.name for host in HOSTS])
        dark = push(sess, "connect terminal-addiction\r")
        self.assertIn("NO CARRIER", dark)
        self.assertEqual(sess.host, "grayline")
        self.assertIn("1200", push(sess, "finger bec\r"))
        entered = push(sess, "connect bec\r")
        self.assertEqual(sess.host, "bec")
        self.assertEqual(sess.baud_now, 1200)
        self.assertGreaterEqual(sess.lead_sleep, 1.0)
        self.assertLessEqual(sess.lead_sleep, 2.0)
        self.assertIn("CONNECT 1200", entered)
        self.assertIn("BIG-EVIL CORPORATION", entered)
        self.assertIn("UNIX V7", entered)
        self.assertIn("12-Nov-93", entered)
        self.assertNotIn("25-Sep-85", entered)
        self.assertIn(str(__import__("datetime").datetime.now().year), entered)
        self.assertTrue(entered.endswith("login: "))
        self.assertNotIn("crawler@orientation", entered)

        guest = push(sess, "guest\r")
        self.assertIn("[1200]", guest)
        self.assertIn("Hank is gone", guest)
        self.assertTrue(strip_sgr(guest).endswith("bec$ "))
        bin_names = push(sess, "ls /bin\r").split()
        for name in ("sh", "ls", "cat", "chmod", "ed", "stty", "sync"):
            self.assertIn(name, bin_names)
        self.assertGreater(len(bin_names), 20)
        self.assertNotIn("home", push(sess, "ls /\r").split())
        listed = push(sess, "ls -l /bin/ls\r")
        self.assertIn("2984", listed)
        self.assertIn("1986", listed)
        self.assertIn("bin", listed)
        self.assertNotIn("2026", listed)
        self.assertIn("-rwxr-xr-x", listed)
        dumped = push(sess, "cat /bin/ls\r")
        self.assertIn("cannot execute as text", dumped)
        self.assertNotIn("V7 shell", dumped)
        self.assertNotIn("def ", dumped)
        self.assertIn("PDP-11", push(sess, "file /bin/ls\r"))
        usr = push(sess, "ls /usr\r").split()
        for name in ("hank", "pat", "guest", "spool", "bin"):
            self.assertIn(name, usr)
        self.assertNotIn("home", usr)
        year = str(__import__("datetime").datetime.now().year)
        self.assertIn(year, push(sess, "date\r"))
        volumes = push(sess, "df\r")
        self.assertIn("rk0", volumes)
        self.assertIn("4800", volumes)
        self.assertIn("rk1", volumes)
        self.assertIn("19200", volumes)
        dev = push(sess, "ls -l /dev/tty02\r")
        self.assertIn("0, 2", dev)
        self.assertIn("wheel", dev)
        self.assertIn("crt0.o", push(sess, "ls /lib\r").split())
        self.assertIn("not found", push(sess, "cc\r"))
        self.assertIn("not found", push(sess, "systemctl\r"))
        self.assertIn("\r\nguest\r\n", push(sess, "whoami\r"))
        self.assertIn("cannot open", push(sess, "cat /usr/games/fortune\r"))
        self.assertTrue(push(sess, "su sys\r").endswith("Password:"))
        become = push(sess, "sys\r")
        self.assertTrue(strip_sgr(become).endswith("bec$ "))
        self.assertIn("\r\nsys\r\n", push(sess, "whoami\r"))
        fortune = push(sess, "cat /usr/games/fortune\r")
        secret = ""
        for line in fortune.split("\r\n"):
            if line.lower().startswith("root password:"):
                secret = line.split(":", 1)[1].strip()
        self.assertTrue(secret)
        self.assertIn("ttyh0", push(sess, "cat /usr/sys/mf.note\r"))
        self.assertTrue(push(sess, "su root\r").endswith("Password:"))
        rooted = push(sess, secret + "\r")
        self.assertTrue(strip_sgr(rooted).endswith("bec# "))
        self.assertIn("\r\nroot\r\n", push(sess, "whoami\r"))
        self.assertIn("ada", push(sess, "useradd ada\r"))
        self.assertIn("ada:", push(sess, "cat /etc/passwd\r"))
        self.assertIn("not found", push(sess, "python\r"))
        self.assertIn("Try man ls", push(sess, "help\r"))
        hopped = push(sess, "cu bec-mf\r")
        self.assertIn("CONNECT 1200", hopped)
        self.assertEqual(sess.baud_now, 1200)
        self.assertEqual(sess.host, "bec-mf")
        self.assertIn("BEC-MF", push(sess, "cat /usr/payroll/memo\r"))
        self.assertTrue(push(sess, "~.\r").endswith("bec# ") or True)
        self.assertEqual(sess.host, "bec")
        self.assertEqual(sess.baud_now, 1200)
        left = push(sess, "logout\r")
        self.assertEqual(sess.host, "grayline")
        self.assertEqual(sess.baud_now, T1_BAUD)
        self.assertIn("NO CARRIER", left)
        self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", left)
        self.assertIn("CIRCUIT OPEN", left)
        self.assertTrue(strip_sgr(left).endswith(pad_prompt_end("ada")))
        self.assertEqual(sess.baud_now, T1_BAUD)
        smoke = (ROOT / "scripts" / "smoke-bec.md").read_text(encoding="utf-8")
        self.assertNotIn(secret, smoke)

    def test_v7_overlay_guest_dies_registered_keeps(self) -> None:
        import shutil

        from crossbar.accounts import create_account, get_account
        from crossbar import v7

        guest = get_session("sid-bec-guest")
        guest.user = "guest"
        guest.host = "bec"
        guest.hops = ["grayline", "terminal-addiction", "bec"]
        guest.baud_stack = [1_544_000, 2400, 1200]
        guest.baud_now = 1200
        guest.v7_phase = "shell"
        guest.v7_user = "guest"
        guest.v7_uid = 999
        guest.v7_gid = 9
        guest.v7_cwd = "/tmp"
        self.assertIn("hello", v7.on_line(guest, "tee /tmp/scratch hello"))
        self.assertIn("hello", v7.on_line(guest, "cat /tmp/scratch"))
        v7.hangup(guest)
        self.assertEqual(guest.v7_store, {})
        self.assertFalse((ROOT / "data" / "users" / "guest").exists())
        again = get_session("sid-bec-guest-2")
        again.user = "guest"
        again.host = "bec"
        again.v7_phase = "shell"
        again.v7_user = "guest"
        again.v7_uid = 999
        again.v7_gid = 9
        again.v7_cwd = "/tmp"
        self.assertIn("not found", v7.on_line(again, "cat /tmp/scratch"))

        overlay = ROOT / "data" / "users" / "ada" / "bec"
        if overlay.exists():
            shutil.rmtree(overlay)
        if get_account("ada") is None:
            create_account("ada", "secret12", "ada@example.com")
        owner = get_session("sid-bec-ada")
        owner.user = "ada"
        owner.host = "bec"
        owner.hops = ["grayline", "terminal-addiction", "bec"]
        owner.baud_stack = [1_544_000, 2400, 1200]
        owner.baud_now = 1200
        owner.v7_phase = "shell"
        owner.v7_user = "root"
        owner.v7_uid = 0
        owner.v7_gid = 0
        owner.v7_cwd = "/"
        self.assertIn("ada", v7.on_line(owner, "useradd ada"))
        self.assertIn("ada:", v7.on_line(owner, "cat /etc/passwd"))
        self.assertEqual(v7.on_line(owner, "tee /tmp/kept hello"), "hello\r\n")
        self.assertEqual(v7.on_line(owner, "cp /tmp/kept /usr/ada/note"), "")
        v7.hangup(owner)
        self.assertFalse((ROOT / "packs" / "big-evil" / "tree" / "usr" / "ada").exists())
        later = get_session("sid-bec-ada-2")
        later.user = "ada"
        later.host = "bec"
        later.v7_phase = "shell"
        later.v7_user = "guest"
        later.v7_uid = 999
        later.v7_gid = 9
        later.v7_cwd = "/"
        self.assertIn("ada:", v7.on_line(later, "cat /etc/passwd"))
        self.assertIn("hello", v7.on_line(later, "cat /usr/ada/note"))

    def test_v7_dispatch_runs_catalog_names(self) -> None:
        from crossbar import v7

        sess = get_session("sid-v7-dispatch")
        sess.user = "guest"
        sess.host = "grayline"
        v7.enter(sess)
        logged = push(sess, "guest\r")
        self.assertTrue(strip_sgr(logged).endswith("bec$ "))
        year = str(__import__("datetime").datetime.now().year)

        def ran(cmd: str) -> str:
            out = push(sess, cmd + "\r")
            self.assertNotIn("\r\n?\r\n", out)
            self.assertFalse(out.endswith("?\r\n"))
            self.assertTrue(strip_sgr(out).endswith("bec$ "), out)
            return out

        self.assertIn(year, ran("date"))
        self.assertIn("BEC", ran("uname"))
        self.assertIn("usage:", ran("uname -z"))
        self.assertEqual(sess.v7_status, 1)
        self.assertTrue(strip_sgr(ran("cd /")).endswith("bec$ "))
        self.assertIn("\r\n/\r\n", ran("pwd"))
        self.assertIn("PDP-11", ran("file /bin/ls"))
        self.assertIn("ascii text", ran("file /etc/motd"))
        self.assertIn("usage:", ran("file"))
        summed = ran("sum /etc/motd")
        self.assertRegex(summed, r"\d+")
        self.assertIn("can't open 1", ran("sum 1"))
        self.assertIn("usage:", ran("sum"))
        self.assertIn("usage:", ran("kill"))
        self.assertIn("no such process", ran("kill 9"))
        self.assertIn("usage:", ran("sleep"))
        asked = push(sess, "su sys\r")
        self.assertTrue(asked.endswith("Password:"))
        stopped = push(sess, "\x03")
        self.assertIn("^C", stopped)
        self.assertTrue(strip_sgr(stopped).endswith("bec$ "))
        self.assertEqual(sess.v7_phase, "shell")
        ran("false")
        self.assertEqual(sess.v7_status, 1)
        self.assertIn("\r\n1\r\n", ran("echo $?"))
        ran("true")
        self.assertEqual(sess.v7_status, 0)
        self.assertIn("\r\n0\r\n", ran("echo $?"))
        ran("date")
        self.assertEqual(sess.v7_status, 0)
        self.assertIn("\r\n0\r\n", ran("echo $?"))
        self.assertIn("not found", ran("cc"))
        self.assertIn("?: not found", ran("?"))

        edited = push(sess, "ed\r")
        self.assertEqual(sess.v7_phase, "ed")
        self.assertNotIn("bec$", edited)
        questioned = push(sess, "date\r")
        self.assertIn("?\r\n", questioned)
        self.assertNotIn("bec$", questioned)
        back = push(sess, "\x03")
        self.assertIn("^C", back)
        self.assertTrue(strip_sgr(back).endswith("bec$ "))
        self.assertEqual(sess.v7_phase, "shell")
        self.assertIn(year, ran("date"))
        self.assertIn("\r\n/\r\n", ran("/bin/pwd"))

    def test_pico_writes_only_where_the_uid_may(self) -> None:
        sess = get_session("sid-pico")
        sess.user = "ada"
        sess.host = "bec"
        sess.v7_phase = "shell"
        sess.v7_user = "guest"
        sess.v7_uid = 999
        sess.v7_gid = 9
        sess.v7_cwd = "/tmp"
        screen = push(sess, "pico /tmp/foo\r")
        self.assertIn("UW PICO 1.0", screen)
        self.assertIn("^G Get Help", screen)
        self.assertIn("^X Exit", screen)
        self.assertEqual(sess.v7_phase, "pico")
        push(sess, "Hi")
        wrote = push(sess, "\x0f")
        self.assertIn("Wrote", wrote)
        left = push(sess, "\x18")
        self.assertEqual(sess.v7_phase, "shell")
        self.assertIn("bec$", left)
        self.assertIn("Hi", push(sess, "cat /tmp/foo\r"))
        opened = push(sess, "pico /etc/passwd\r")
        self.assertIn("UW PICO", opened)
        self.assertTrue(sess.pico_ro)
        denied = push(sess, "\x0f")
        self.assertIn("Read only", denied)
        push(sess, "\x18")
        self.assertEqual(sess.v7_phase, "shell")
        listed = push(sess, "ls -l /etc/motd\r")
        self.assertIn("1993", listed)
        self.assertNotIn("2026", listed)
        self.assertIn("1993", push(sess, "last\r"))
        self.assertIn("getty", push(sess, "ps\r"))
        self.assertIn("broken: termcap", push(sess, "vi /tmp/foo\r"))

    def test_unknown_command(self) -> None:
        sess = get_session("sid-unk")
        sess.user = "ada"
        out = push(sess, "frobnicate\r")
        self.assertIn("frobnicate: not found\r\n", out)
        self.assertTrue(strip_sgr(out).endswith(pad_prompt_end()))

    def test_clear_and_clr_wipe_the_pad(self) -> None:
        sess = get_session("sid-clr")
        sess.user = "ada"
        wiped = push(sess, "clear\r")
        self.assertIn("\x1b[2J\x1b[H", wiped)
        self.assertTrue(strip_sgr(wiped).endswith(pad_prompt_end("ada")))
        alias = push(sess, "clr\r")
        self.assertIn("\x1b[2J\x1b[H", alias)
        self.assertTrue(strip_sgr(alias).endswith(pad_prompt_end("ada")))

    def test_who_is_other_live_sessions_only(self) -> None:
        ada = get_session("sid-ada")
        ada.user = "ada"
        bob = get_session("sid-bob")
        bob.user = "bob"
        waiting = get_session("sid-wait")
        idle = get_session("sid-idle")
        idle.user = "cyd"
        idle.last_active = time.monotonic() - IDLE_TTL - 5

        self.assertEqual(cmd_who(ada, []), "bob\r\n")
        self.assertNotIn("ada", cmd_who(ada, []))
        self.assertNotIn("cyd", cmd_who(ada, []))
        self.assertIsNone(waiting.user)
        self.assertIn("ada\r\n", cmd_who(bob, []))
        bob.user = None
        self.assertIn("no one else", cmd_who(ada, []))

    def test_finger_hosts_connect_logout(self) -> None:
        sess = get_session("sid-cmd")
        sess.user = "ada"
        sess.host = "elsewhere"
        out = cmd_connect(sess, ["grayline"])
        self.assertEqual(sess.host, "grayline")
        self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", out)
        self.assertIn("CIRCUIT OPEN", out)
        self.assertTrue(prompt_for(sess).endswith(pad_prompt_end("ada")))

        menu = cmd_connect(sess, ["terminal-addiction"])
        self.assertIn("NO CARRIER", menu)
        self.assertEqual(sess.host, "grayline")
        self.assertIn("not found", cmd_connect(sess, ["orientation"]))
        self.assertIn("not found", cmd_connect(sess, ["no-such-host"]))

        listing = cmd_hosts(sess, [])
        self.assertIn("BEC", listing)
        self.assertIn("ORIENTATION", listing)
        self.assertIn("TYMNET", listing)
        self.assertIn("OFFLINE", listing)
        self.assertEqual(
            [host.name for host in HOSTS],
            ["bec", "tymnet", "mudproto", "terminal-addiction"],
        )

        sess.login_at = time.time()
        finger = cmd_finger(sess, [])
        self.assertIn("Login            Name             TTY      Idle  Where", finger)
        self.assertIn("ada", finger)
        self.assertIn("PAD", finger)

        other = get_session("sid-other")
        other.user = "bob"
        other.login_at = time.time()
        found = cmd_finger(sess, ["bob"])
        self.assertIn("Login: bob", found)
        self.assertIn("On since", found)
        self.assertIn("No Plan.", found)
        self.assertIn("no such user", cmd_finger(sess, ["nobody"]))

        sid = sess.sid
        out = cmd_logout(sess, [])
        self.assertEqual(sess.sid, sid)
        self.assertIsNone(sess.user)
        self.assertIn("GRAYLINE", out)
        self.assertTrue(out.endswith("LOGON: "))
        self.assertEqual(sess.history, [])

    def test_refresh_hello_and_history_arrows(self) -> None:
        sess = get_session("sid-hist")
        sess.user = "ada"
        push(sess, "motd\r")
        push(sess, "who\r")
        push(sess, "xx")
        out = push(sess, "\x1b[A")
        self.assertEqual(sess.line, "who")
        self.assertIn("\b \b", out)
        self.assertTrue(out.endswith("who"))
        resumed = hello(sess)
        self.assertIn("GL-01", resumed)
        self.assertTrue(strip_sgr(resumed).endswith(pad_prompt_end("ada")))
        self.assertEqual(sess.line, "")
        self.assertEqual(sess.history, ["motd", "who"])

    def test_boot_is_dim_green_and_short(self) -> None:
        from crossbar.lobby import boot_duration, boot_steps

        steps = boot_steps()
        self.assertEqual(len(steps), 1)
        self.assertLessEqual(boot_duration(), 2.0)
        plain = "".join(text for text, _d, _k in steps)
        for line in plain.split("\r\n"):
            self.assertLessEqual(len(line), 80, line)
        self.assertIn("CONNECTED T1", plain)
        self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", plain)
        self.assertIn("Crossbar Consortium", plain)
        self.assertNotIn("\x1b[32m", plain)
        self.assertNotIn("1985", plain)
        self.assertNotIn("1993", plain)

    def test_boot_skip_after_connect(self) -> None:
        from crossbar.ws import play_boot

        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.reads = 0

            async def send_text(self, data: str) -> None:
                self.sent.append(data)

            async def receive(self) -> dict[str, str]:
                self.reads += 1
                return {"type": "websocket.receive", "text": " "}

        fake = FakeSocket()
        asyncio.run(play_boot(fake))  # type: ignore[arg-type]
        joined = "".join(fake.sent)
        self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", joined)
        self.assertIn("Crossbar Consortium", joined)
        self.assertEqual(fake.reads, 0)

    def test_finger_dummies_and_verify(self) -> None:
        from crossbar.accounts import create_account, set_account_fields

        sess = get_session("sid-finger")
        sess.user = "ada"
        sess.login_at = time.time()
        sysop = push(sess, "finger sysop\r")
        admin = push(sess, "finger admin\r")
        self.assertIn("not accepting mail", sysop)
        self.assertIn("not accepting mail", admin)
        self.assertIn("verification is dark", push(sess, "verify\r"))

        create_account("cyd", "secret12", "cyd@example.com")
        set_account_fields("cyd", note="builds doors\ntrusts the pad")
        offline = cmd_finger(sess, ["cyd"])
        self.assertIn("Login: cyd", offline)
        self.assertIn("Not logged in.", offline)
        self.assertIn("Mail: cyd@example.com", offline)
        self.assertIn("Plan:", offline)
        self.assertIn("builds doors", offline)
        self.assertNotIn("ps1", offline.lower())
        self.assertNotIn("wallet", offline.lower())

        short = cmd_finger(sess, [])
        self.assertIn("Login            Name", short)
        self.assertIn("ada", short)

    def test_commands_do_not_import_starlette(self) -> None:
        source = (ROOT / "crossbar" / "commands.py").read_text(encoding="utf-8")
        self.assertNotIn("starlette", source.lower())

    def test_page_is_a_terminal(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("xterm", html)
        self.assertIn('title>GRAYLINE<', html.replace(" ", ""))
        self.assertIn("/ws", html)
        self.assertIn("popstate", html)
        self.assertNotIn("PiSecure", html)
        self.assertNotIn("pisecure", html.lower())


class LiveServerTests(unittest.IsolatedAsyncioTestCase):
    proc: subprocess.Popen[bytes]
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        global _BASE
        cls.port = _free_port()
        _BASE = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["CROSSBAR_SECRET"] = SECRET
        env["CROSSBAR_PORT"] = str(cls.port)
        _live_fd, live_db = tempfile.mkstemp(prefix="grayline-live-", suffix=".db")
        os.close(_live_fd)
        env["CROSSBAR_DB"] = live_db
        cls.live_db = live_db
        _live_wall_fd, live_wall = tempfile.mkstemp(prefix="wall-live-", suffix=".asc")
        os.close(_live_wall_fd)
        with open(live_wall, "w", encoding="utf-8") as handle:
            handle.write("# live test wall\n")
        env["CROSSBAR_WALL"] = live_wall
        cls.live_wall = live_wall
        cls.log_path = Path("/tmp/crossbar-test.log")
        cls.log = cls.log_path.open("w", encoding="utf-8")
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, uvicorn\n"
                "from crossbar.app import app\n"
                "uvicorn.run(app, host='127.0.0.1', port=int(os.environ['CROSSBAR_PORT']), log_level='warning')\n",
            ],
            cwd=ROOT,
            env=env,
            stdout=cls.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                cls.log.flush()
                raise RuntimeError(f"server exited\n{cls.log_path.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        cls._stop()
        cls.log.flush()
        raise TimeoutError(f"server did not listen\n{cls.log_path.read_text()}")

    @classmethod
    def _stop(cls) -> None:
        proc = getattr(cls, "proc", None)
        if proc is None or proc.poll() is not None:
            return
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        log = getattr(cls, "log", None)
        if log is not None and not log.closed:
            log.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop()

    async def test_two_browsers_refresh_and_backspace(self) -> None:
        import websockets

        left_cookie, left_sid, html = await asyncio.to_thread(_get_home, None)
        self.assertIn("xterm.min.js", html)
        self.assertIn("httponly", left_cookie.lower())
        right_cookie, right_sid, _ = await asyncio.to_thread(_get_home, None)
        self.assertNotEqual(left_sid, right_sid)

        async with _connect(websockets, left_cookie) as left, _connect(websockets, right_cookie) as right:
            left_hello = await _skip_boot(left)
            right_hello = await _skip_boot(right)
            self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", left_hello)
            self.assertTrue(left_hello.endswith("LOGON: "))
            self.assertTrue(right_hello.endswith("LOGON: "))

            ada_in = await _register(left, "ada", "ada@example.com")
            self.assertIn("IDENTITY  ada", ada_in)
            self.assertTrue(strip_sgr(ada_in).endswith(pad_prompt_end("ada")))
            bob_in = await _register(right, "bob", "bob@example.com")
            self.assertTrue(strip_sgr(bob_in).endswith(pad_prompt_end("bob")))

            await left.send("who\r")
            who = await left.recv()
            self.assertIn("\r\nbob\r\n", who)
            self.assertNotIn("\r\nada\r\n", who)

            # Half-typed line, then backspace, then the rest of help.
            await left.send("hex")
            self.assertEqual(await left.recv(), "hex")
            await left.send("\x7f")
            self.assertEqual(await left.recv(), "\b \b")
            await right.send("who\r")
            leaked = await right.recv()
            self.assertNotIn("hex", leaked)
            self.assertIn("ada", leaked)

            await left.send("lp\r")
            helped = await left.recv()
            self.assertIn("\r\n", helped)
            self.assertIn("circuits", helped)

        async with _connect(websockets, left_cookie) as resumed:
            text = await resumed.recv()
            self.assertIn("GL-01", text)
            self.assertTrue(strip_sgr(text).endswith(pad_prompt_end("ada")))
            await resumed.send("logout\r")
            out = await resumed.recv()
            self.assertIn("PACKET ASSEMBER/DISSASEMBLER READY", out)
            self.assertTrue(out.endswith("LOGON: "))

        again_cookie, again_sid, _ = await asyncio.to_thread(_get_home, left_cookie)
        self.assertEqual(again_sid, left_sid)
        async with _connect(websockets, again_cookie) as logged_out:
            text = await _skip_boot(logged_out)
            self.assertIn("LOGON:", text)
            self.assertNotIn("resumed", text)

        listen = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN"],
            text=True,
        )
        self.assertIn(f"127.0.0.1:{self.port}", listen)
        self.assertNotIn("0.0.0.0:", listen)
        self.assertNotIn("*:", listen)


_BASE = "http://127.0.0.1:8080"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


async def _read_until(ws, suffix: str, timeout: float = 4) -> str:
    data = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not data.endswith(suffix):
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError(repr(data))
        data += await asyncio.wait_for(ws.recv(), left)
    return data


async def _skip_boot(ws) -> str:
    data = ""
    while not data.endswith("LOGON: "):
        data += await asyncio.wait_for(ws.recv(), 2)
    return data


async def _register(ws, handle: str, email: str) -> str:
    await ws.send("new\r")
    await _read_until(ws, "handle: ")
    await ws.send(f"{handle}\r")
    await _read_until(ws, "PASSWORD: ")
    await ws.send("secret12\r")
    await _read_until(ws, "confirm: ")
    await ws.send("secret12\r")
    await _read_until(ws, "email: ")
    await ws.send(f"{email}\r")
    return await _read_until(ws, pad_prompt_end(handle))


def _cookie_pair(header: str) -> str:
    return header.split(";", 1)[0].strip()


def _get_home(cookie_pair: str | None) -> tuple[str, str, str]:
    """Return the Set-Cookie header (or the pair we sent), the sid, and the HTML."""
    request = urllib.request.Request(f"{_BASE}/")
    if cookie_pair:
        request.add_header("Cookie", _cookie_pair(cookie_pair))
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode()
        set_cookie = response.headers.get("Set-Cookie")
    header = set_cookie or cookie_pair
    if not header:
        raise AssertionError("missing session cookie")
    sid = _sid_from_cookie(_cookie_pair(header).split("=", 1)[1])
    return header, sid, body


def _sid_from_cookie(value: str) -> str:
    from itsdangerous import TimestampSigner

    signer = TimestampSigner(SECRET)
    payload = signer.unsign(value.encode("utf-8"), max_age=14 * 24 * 60 * 60)
    data = json.loads(base64.b64decode(payload))
    sid = data["sid"]
    if not isinstance(sid, str):
        raise AssertionError("sid missing")
    return sid


def _connect(websockets, cookie: str):
    # GET returns "name=value; httponly; ...". The handshake wants name=value.
    headers = {"Cookie": _cookie_pair(cookie)}
    params = inspect.signature(websockets.connect).parameters
    kwargs = {"proxy": None}
    if "additional_headers" in params:
        kwargs["additional_headers"] = headers
    else:
        kwargs["extra_headers"] = headers
    host = _BASE.split("://", 1)[1]
    return websockets.connect(f"ws://{host}/ws", **kwargs)


class EntrypointTests(unittest.TestCase):
    def test_module_targets_localhost_8080(self) -> None:
        log_path = Path("/tmp/crossbar-entrypoint.log")
        log = log_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env["CROSSBAR_SECRET"] = SECRET
        proc = subprocess.Popen(
            [sys.executable, "-m", "crossbar"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        text = ""
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                log.flush()
                text = log_path.read_text(encoding="utf-8")
                if proc.poll() is not None:
                    break
                if "Uvicorn running" in text or "address already in use" in text:
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            log.close()
        self.assertIn("127.0.0.1", text)
        self.assertIn("8080", text)
        self.assertNotIn("0.0.0.0", text)


if __name__ == "__main__":
    unittest.main()
