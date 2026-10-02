"""MudProto TCP door — no Grayline identity on the wire."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from crossbar import accounts
from crossbar import mudproto
from crossbar.accounts import create_account
from crossbar.session import SESSIONS, get_session


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakeMud(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.received: list[bytes] = []
        self._halt = threading.Event()
        self.ready = threading.Event()

    def run(self) -> None:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.port))
        server.listen(5)
        server.settimeout(0.2)
        self.ready.set()
        while not self._halt.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            with conn:
                conn.sendall(b"MUDPROTO\nconnected 9600\n\nname:\n")
                conn.settimeout(0.5)
                while not self._halt.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    self.received.append(chunk)
                    if b"QUIT\n" in chunk or b"~.\n" in chunk:
                        conn.sendall(b"CARRIER LOST\n")
                        break
                    if b"hello\n" in chunk:
                        conn.sendall(b"heard hello\n")
        server.close()

    def stop(self) -> None:
        self._halt.set()


class MudProtoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_db = os.environ.get("CROSSBAR_DB")
        fd, path = tempfile.mkstemp(prefix="mudproto-", suffix=".db")
        os.close(fd)
        self._db = path
        os.environ["CROSSBAR_DB"] = path
        if accounts._conn is not None:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        accounts.connect()
        create_account("ada", "secret12", "ada@example.com")
        self.port = _free_port()
        self._prev_host = os.environ.get("MUDPROTO_HOST")
        self._prev_port = os.environ.get("MUDPROTO_PORT")
        os.environ["MUDPROTO_HOST"] = "127.0.0.1"
        os.environ["MUDPROTO_PORT"] = str(self.port)
        self.server = _FakeMud(self.port)
        self.server.start()
        self.assertTrue(self.server.ready.wait(2.0))

    def tearDown(self) -> None:
        self.server.stop()
        self.server.join(timeout=2.0)
        if self._prev_host is None:
            os.environ.pop("MUDPROTO_HOST", None)
        else:
            os.environ["MUDPROTO_HOST"] = self._prev_host
        if self._prev_port is None:
            os.environ.pop("MUDPROTO_PORT", None)
        else:
            os.environ["MUDPROTO_PORT"] = self._prev_port
        if accounts._conn is not None:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        if self._prev_db is None:
            os.environ.pop("CROSSBAR_DB", None)
        else:
            os.environ["CROSSBAR_DB"] = self._prev_db
        try:
            os.unlink(self._db)
        except OSError:
            pass
        SESSIONS.clear()

    def test_health_expects_banner(self) -> None:
        ok, text = mudproto.health(timeout=1.0)
        self.assertTrue(ok)
        self.assertIn("MUDPROTO", text)
        self.assertIn("name:", text.lower())

    def test_health_down(self) -> None:
        os.environ["MUDPROTO_PORT"] = str(self.port + 50)
        ok, _text = mudproto.health(timeout=0.3)
        self.assertFalse(ok)

    def test_attach_pipes_without_identity(self) -> None:
        async def run() -> None:
            SESSIONS.clear()
            sess = get_session("mud-ada")
            sess.user = "ada"
            sess.host = "grayline"
            out: list[str] = []

            async def send(chunk: str) -> None:
                out.append(chunk)

            dial = mudproto.begin_dial(sess)
            self.assertIn("DIALING", dial)
            self.assertEqual(sess.host, "mudproto")
            self.assertTrue(sess.mud_pending_connect)
            empty = await mudproto.attach(sess, send)
            self.assertEqual(empty, "")
            # Wait for banner from fake mud.
            for _ in range(50):
                if any("MUDPROTO" in chunk for chunk in out):
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(any("MUDPROTO" in chunk for chunk in out))
            self.assertTrue(any("name:" in chunk.lower() for chunk in out))
            mudproto.on_line(sess, "ada")  # character name — not !account
            mudproto.on_line(sess, "hello")
            for _ in range(50):
                if any(b"hello\n" in chunk for chunk in self.server.received):
                    break
                await asyncio.sleep(0.05)
            wire = b"".join(self.server.received)
            self.assertIn(b"ada\n", wire)
            self.assertIn(b"hello\n", wire)
            self.assertNotIn(b"!name", wire)
            self.assertNotIn(b"!account", wire)
            # Hangup via QUIT
            mudproto.on_line(sess, "QUIT")
            self.assertTrue(sess.mud_pending_hangup)
            sess.mud_pending_hangup = False
            text = await mudproto.hangup(sess, announce=True)
            self.assertIn("CARRIER LOST", text)
            self.assertEqual(sess.host, "grayline")
            self.assertIsNone(sess.mud_writer)

        asyncio.run(run())

    def test_connect_no_carrier_when_down(self) -> None:
        async def run() -> None:
            os.environ["MUDPROTO_PORT"] = str(self.port + 77)
            sess = get_session("mud-down")
            sess.user = "ada"
            sess.host = "grayline"
            mudproto.begin_dial(sess)

            async def send(_chunk: str) -> None:
                return None

            text = await mudproto.attach(sess, send)
            self.assertIn("NO CARRIER", text)
            self.assertEqual(sess.host, "grayline")

        asyncio.run(run())

    def test_hosts_lists_mudproto(self) -> None:
        from crossbar.lobby import pad_hosts_text
        from crossbar.packs import get_pack

        names = [host.name for host in get_pack("grayline").hosts]
        self.assertIn("mudproto", names)
        listing = pad_hosts_text()
        self.assertIn("MUDPROTO", listing)


if __name__ == "__main__":
    unittest.main()
