"""Local GROUPS store and pad verb."""

from __future__ import annotations

import os
import tempfile
import unittest

from crossbar import accounts
from crossbar import groups
from crossbar.accounts import create_account


class GroupsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get("CROSSBAR_DB")
        fd, path = tempfile.mkstemp(prefix="grayline-groups-", suffix=".db")
        os.close(fd)
        self._path = path
        os.environ["CROSSBAR_DB"] = path
        if accounts._conn is not None:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        accounts.connect()
        create_account("ada", "secret12", "ada@example.com")
        create_account("bob", "secret12", "bob@example.com")

    def tearDown(self) -> None:
        if accounts._conn is not None:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        if self._previous is None:
            os.environ.pop("CROSSBAR_DB", None)
        else:
            os.environ["CROSSBAR_DB"] = self._previous
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def test_seed_groups(self) -> None:
        names = {row.name for row in groups.list_groups()}
        self.assertEqual(
            names,
            {"grayline.general", "grayline.doors", "grayline.sysop"},
        )

    def test_post_headers_read_next_prev(self) -> None:
        n1 = groups.post(
            group="grayline.general",
            from_handle="ada",
            subject="hello",
            body="first post",
        )
        self.assertEqual(n1, 1)
        n2 = groups.post(
            group="grayline.general",
            from_handle="bob",
            subject="re: hello",
            body="second",
        )
        self.assertEqual(n2, 2)
        heads = groups.headers("grayline.general", limit=10)
        self.assertEqual([h.number for h in heads], [2, 1])
        art = groups.get_article("grayline.general", 1)
        assert art is not None
        self.assertEqual(art.from_handle, "ada")
        self.assertIn("@", art.message_id)
        self.assertEqual(art.body, "first post")
        nxt = groups.next_article("grayline.general", 1)
        assert nxt is not None
        self.assertEqual(nxt.number, 2)
        prev = groups.prev_article("grayline.general", 2)
        assert prev is not None
        self.assertEqual(prev.number, 1)

    def test_guest_and_sysop_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "logon required"):
            groups.post(
                group="grayline.general",
                from_handle="guest",
                subject="x",
                body="y",
            )
        with self.assertRaisesRegex(ValueError, "sysop only"):
            groups.post(
                group="grayline.sysop",
                from_handle="ada",
                subject="x",
                body="y",
            )
        n = groups.post(
            group="grayline.sysop",
            from_handle="sysop",
            subject="notice",
            body="maintenance window",
            force_sysop=True,
        )
        self.assertEqual(n, 1)

    def test_unread_watermark(self) -> None:
        groups.post(
            group="grayline.doors",
            from_handle="ada",
            subject="one",
            body="a",
        )
        groups.post(
            group="grayline.doors",
            from_handle="bob",
            subject="two",
            body="b",
        )
        self.assertEqual(groups.unread_count("ada", "grayline.doors"), 2)
        groups.mark_read("ada", "grayline.doors", 1)
        self.assertEqual(groups.unread_count("ada", "grayline.doors"), 1)
        groups.mark_read("ada", "grayline.doors", 2)
        self.assertEqual(groups.unread_count("ada", "grayline.doors"), 0)
        self.assertEqual(groups.unread_count("guest", "grayline.doors"), 0)

    def test_pad_groups_flow(self) -> None:
        from crossbar.session import SESSIONS, get_session
        from crossbar.ws import push

        SESSIONS.clear()
        ada = get_session("groups-ada")
        ada.user = "ada"
        ada.host = "grayline"
        ada.phase = "shell"
        listed = push(ada, "groups\r")
        self.assertIn("grayline.general", listed)
        selected = push(ada, "groups grayline.general\r")
        self.assertIn("Group grayline.general", selected)
        started = push(ada, "groups post hello world\r")
        self.assertIn("Compose to grayline.general", started)
        self.assertEqual(ada.phase, "group_body")
        done = push(ada, "line one\r.\r")
        self.assertIn("posted 1 to grayline.general", done)
        headers = push(ada, "groups headers\r")
        self.assertIn("hello world", headers)
        read = push(ada, "groups read 1\r")
        self.assertIn("From: ada", read)
        self.assertIn("line one", read)
        self.assertIn("Message-ID:", read)
        guest = get_session("groups-guest")
        guest.user = "guest"
        guest.host = "grayline"
        guest.phase = "shell"
        push(guest, "groups grayline.general\r")
        self.assertIn("logon required", push(guest, "groups post\r"))
        push(ada, "groups grayline.sysop\r")
        self.assertIn("sysop only", push(ada, "groups post\r"))


if __name__ == "__main__":
    unittest.main()
