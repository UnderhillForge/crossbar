"""Operator console. The public pad must not link here."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

_fd, _DB = tempfile.mkstemp(prefix="admin-", suffix=".db")
os.close(_fd)
_pad_fd, _PAD_DB = tempfile.mkstemp(prefix="admin-pad-", suffix=".db")
os.close(_pad_fd)
_SITE = tempfile.mkdtemp(prefix="admin-site-")
os.environ["CROSSBAR_ADMIN_DB"] = _DB
os.environ["CROSSBAR_ADMIN_AUDIT"] = str(Path(_SITE) / "audit.log")
os.environ["CROSSBAR_SITE"] = _SITE
os.environ["CROSSBAR_DB"] = _PAD_DB

from starlette.testclient import TestClient  # noqa: E402

from crossbar import accounts  # noqa: E402
from crossbar.adminapp import build_admin_app  # noqa: E402
from crossbar.lobby import pad_hosts_text  # noqa: E402
from crossbar.operators import issue_setup_token  # noqa: E402

if accounts._conn is not None:
    accounts._conn.close()
    accounts._conn = None
    accounts._conn_path = None


class AdminTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CROSSBAR_SITE"] = _SITE
        os.environ["CROSSBAR_DB"] = _PAD_DB
        if accounts._conn is not None and accounts._conn_path != _PAD_DB:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        runtime = Path(_SITE) / "runtime.json"
        if runtime.exists():
            runtime.unlink()

    def tearDown(self) -> None:
        runtime = Path(_SITE) / "runtime.json"
        if runtime.exists():
            runtime.unlink()

    def test_port_root_is_unauthorized_and_toggle_reaches_hosts(self) -> None:
        page = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()
        self.assertNotIn("8081", page)
        self.assertNotIn("/o/", page)

        client = TestClient(build_admin_app())
        self.assertEqual(client.get("/").status_code, 401)
        self.assertEqual(client.get("/o/sessions").status_code, 401)
        self.assertEqual(client.get("/o/overview").status_code, 401)

        token = issue_setup_token()
        self.assertIsNotNone(token)
        created = client.post(
            "/o/setup",
            data={"token": token, "name": "adaops", "password": "correct-horse"},
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)
        signed = client.post(
            "/o/login",
            data={"name": "adaops", "password": "correct-horse"},
            follow_redirects=False,
        )
        self.assertEqual(signed.status_code, 303)
        raw = signed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        client.cookies.set("crossbar_op", raw)
        home = client.get("/o/overview")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Overview", home.text)
        hosts = client.get("/o/hosts")
        csrf = re.search(r'name="csrf" value="([^"]+)"', hosts.text)
        self.assertIsNotNone(csrf)
        assert csrf is not None
        changed = client.post(
            "/o/hosts",
            data={"csrf": csrf.group(1), "name": "bec", "state": "OFFLINE", "title": ""},
            follow_redirects=True,
        )
        self.assertEqual(changed.status_code, 200)
        bec = [line for line in pad_hosts_text().splitlines() if line.startswith("BEC")]
        self.assertTrue(bec and "OFFLINE" in bec[0])
        audit = Path(os.environ["CROSSBAR_ADMIN_AUDIT"]).read_text(encoding="utf-8")
        self.assertIn("host-state", audit)
        self.assertIn("adaops", audit)

        from crossbar.accounts import create_account, get_account
        from crossbar import mail as pad_mail

        if get_account("bobmail") is None:
            create_account("bobmail", "secret12", "bobmail@example.com")
        mail_page = client.get("/o/mail")
        self.assertEqual(mail_page.status_code, 200)
        self.assertIn("broadcast as sysop", mail_page.text)
        csrf_mail = re.search(r'name="csrf" value="([^"]+)"', mail_page.text)
        self.assertIsNotNone(csrf_mail)
        assert csrf_mail is not None
        broadcast = client.post(
            "/o/mail",
            data={
                "csrf": csrf_mail.group(1),
                "subject": "pad notice",
                "body": "hello from console",
                "audience": "handles",
                "handles": "bobmail nobody",
            },
            follow_redirects=True,
        )
        self.assertEqual(broadcast.status_code, 200)
        self.assertIn("sent 1, skipped 1", broadcast.text)
        rows = pad_mail.list_letters("bobmail")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].sender, "sysop")
        self.assertEqual(rows[0].kind, "broadcast")
        self.assertEqual(rows[0].subject, "pad notice")
        self.assertIn("hello from console", rows[0].body)
        audit2 = Path(os.environ["CROSSBAR_ADMIN_AUDIT"]).read_text(encoding="utf-8")
        self.assertIn("mail-broadcast", audit2)

        watch = client.post(
            "/o/config",
            data={
                "csrf": csrf.group(1),
                "add": "1",
                "name": "watcher",
                "password": "correct-horse",
                "role": "watch",
            },
            follow_redirects=False,
        )
        self.assertEqual(watch.status_code, 303)
        other = TestClient(build_admin_app())
        watched = other.post(
            "/o/login",
            data={"name": "watcher", "password": "correct-horse"},
            follow_redirects=False,
        )
        other.cookies.set(
            "crossbar_op",
            watched.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1],
        )
        page = other.get("/o/hosts")
        other_csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert other_csrf is not None
        refused = other.post(
            "/o/hosts",
            data={"csrf": other_csrf.group(1), "name": "bec", "state": "UP", "title": ""},
        )
        self.assertEqual(refused.status_code, 403)


if __name__ == "__main__":
    unittest.main()
