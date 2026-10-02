"""Pad-local mail data layer (PR 1)."""

from __future__ import annotations

import os
import tempfile
import unittest

from crossbar import accounts
from crossbar import mail
from crossbar.accounts import create_account, set_account_fields


class MailApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get("CROSSBAR_DB")
        self._previous_wall = os.environ.get("CROSSBAR_WALL")
        fd, path = tempfile.mkstemp(prefix="grayline-mail-", suffix=".db")
        os.close(fd)
        self._path = path
        os.environ["CROSSBAR_DB"] = path
        wall_fd, wall_path = tempfile.mkstemp(prefix="wall-mail-", suffix=".asc")
        os.close(wall_fd)
        self._wall_path = wall_path
        os.environ["CROSSBAR_WALL"] = wall_path
        with open(wall_path, "w", encoding="utf-8") as handle:
            handle.write("# test wall\n")
        if accounts._conn is not None:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        accounts.connect()
        create_account("ada", "secret12", "ada@example.com")
        create_account("bob", "secret12", "bob@example.com")
        create_account("carol", "secret12", "carol@example.com")

    def tearDown(self) -> None:
        if accounts._conn is not None:
            accounts._conn.close()
            accounts._conn = None
            accounts._conn_path = None
        if self._previous is None:
            os.environ.pop("CROSSBAR_DB", None)
        else:
            os.environ["CROSSBAR_DB"] = self._previous
        if self._previous_wall is None:
            os.environ.pop("CROSSBAR_WALL", None)
        else:
            os.environ["CROSSBAR_WALL"] = self._previous_wall
        for path in (self._path, self._wall_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_schema_exists(self) -> None:
        tables = {
            row[0]
            for row in accounts.connect()
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }
        self.assertIn("mail_messages", tables)
        self.assertIn("mail_copies", tables)

    def test_send_creates_inbox_and_sent_copies(self) -> None:
        mid = mail.send(sender="ada", to="bob", subject="hello", body="hi bob")
        self.assertIsInstance(mid, int)
        bob_inbox = mail.list_letters("bob", "inbox")
        self.assertEqual(len(bob_inbox), 1)
        self.assertEqual(bob_inbox[0].id, mid)
        self.assertEqual(bob_inbox[0].sender, "ada")
        self.assertEqual(bob_inbox[0].to_list, "bob")
        self.assertIsNone(bob_inbox[0].read_at)
        ada_sent = mail.list_letters("ada", "sent")
        self.assertEqual(len(ada_sent), 1)
        self.assertEqual(ada_sent[0].id, mid)
        self.assertIsNotNone(ada_sent[0].read_at)
        self.assertEqual(mail.unread_count("bob"), 1)
        self.assertEqual(mail.unread_count("ada"), 0)

    def test_reject_self_mail(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot send to yourself"):
            mail.send(sender="ada", to="ada", subject="x", body="y")

    def test_reject_reserved_and_unknown(self) -> None:
        with self.assertRaisesRegex(ValueError, "not accepting mail"):
            mail.send(sender="ada", to="sysop", subject="x", body="y")
        with self.assertRaisesRegex(ValueError, "no such user"):
            mail.send(sender="ada", to="nobody", subject="x", body="y")

    def test_empty_subject_allowed_empty_body_not(self) -> None:
        mid = mail.send(sender="ada", to="bob", subject="", body="body only")
        letter = mail.get_letter("bob", mid)
        assert letter is not None
        self.assertEqual(letter.subject, "")
        with self.assertRaisesRegex(ValueError, "empty body"):
            mail.send(sender="ada", to="bob", subject="x", body="   \n  ")

    def test_unread_notice_guest_safe(self) -> None:
        self.assertEqual(mail.unread_notice("guest"), "")
        self.assertEqual(mail.unread_notice(""), "")
        self.assertEqual(mail.unread_notice("bob"), "")
        mail.send(sender="ada", to="bob", subject="one", body="a")
        self.assertEqual(mail.unread_notice("bob"), "You have 1 new letter.\r\n")
        mail.send(sender="ada", to="bob", subject="two", body="b")
        self.assertEqual(mail.unread_notice("bob"), "You have 2 new letters.\r\n")

    def test_mark_read_soft_delete_archive(self) -> None:
        mid = mail.send(sender="ada", to="bob", subject="s", body="b")
        self.assertEqual(mail.unread_count("bob"), 1)
        mail.mark_read("bob", mid)
        self.assertEqual(mail.unread_count("bob"), 0)
        letter = mail.get_letter("bob", mid)
        assert letter is not None
        self.assertIsNotNone(letter.read_at)
        self.assertTrue(mail.archive("bob", mid))
        self.assertEqual(mail.list_letters("bob", "inbox"), [])
        archived = mail.list_letters("bob", "archive")
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].id, mid)
        self.assertTrue(mail.soft_delete("bob", mid))
        self.assertIsNone(mail.get_letter("bob", mid))
        self.assertEqual(mail.list_letters("bob", "archive"), [])

    def test_list_newest_first_excludes_deleted(self) -> None:
        first = mail.send(sender="ada", to="bob", subject="old", body="1")
        second = mail.send(sender="ada", to="bob", subject="new", body="2")
        rows = mail.list_letters("bob", "inbox")
        self.assertEqual([row.id for row in rows], [second, first])
        mail.soft_delete("bob", second)
        rows = mail.list_letters("bob", "inbox")
        self.assertEqual([row.id for row in rows], [first])

    def test_reply_sets_in_reply_to_forward_does_not(self) -> None:
        original = mail.send(sender="ada", to="bob", subject="hello", body="hi")
        reply = mail.send(
            sender="bob",
            to="ada",
            subject="Re: hello",
            body="> hi\nok",
            in_reply_to=original,
        )
        letter = mail.get_letter("ada", reply)
        assert letter is not None
        self.assertEqual(letter.in_reply_to, original)
        fwd = mail.send(
            sender="bob",
            to="carol",
            subject="Fwd: hello",
            body="forwarded",
            in_reply_to=None,
        )
        forwarded = mail.get_letter("carol", fwd)
        assert forwarded is not None
        self.assertIsNone(forwarded.in_reply_to)

    def test_broadcast_all_and_empty(self) -> None:
        n, skipped = mail.broadcast(subject="maint", body="window", handles=None)
        self.assertEqual(n, 3)
        self.assertEqual(skipped, 0)
        for who in ("ada", "bob", "carol"):
            rows = mail.list_letters(who, "inbox")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].sender, "sysop")
            self.assertEqual(rows[0].kind, "broadcast")
            self.assertEqual(rows[0].to_list, "all")
        with self.assertRaises(mail.NoRecipientsError) as raised:
            mail.broadcast(subject="x", body="y", handles=["nobody", "sysop"])
        self.assertEqual(raised.exception.skipped, 2)
        before = accounts.connect().execute("SELECT COUNT(*) AS n FROM mail_messages").fetchone()
        # only the successful broadcast row remains
        self.assertEqual(int(before["n"]), 1)

    def test_broadcast_compact_to_labels(self) -> None:
        n, skipped = mail.broadcast(
            subject="hi", body="two", handles=["ada", "bob", "ghost"]
        )
        self.assertEqual(n, 2)
        self.assertEqual(skipped, 1)
        letter = mail.get_letter("ada", mail.list_letters("ada")[0].id)
        assert letter is not None
        self.assertEqual(letter.to_list, "ada, bob")
        create_account("dave", "secret12", "dave@example.com")
        create_account("erin", "secret12", "erin@example.com")
        n, skipped = mail.broadcast(
            subject="many",
            body="four",
            handles=["ada", "bob", "carol", "dave"],
        )
        self.assertEqual(n, 4)
        letter = mail.list_letters("dave")[0]
        self.assertEqual(letter.to_list, "many")

    def test_disabled_recipient_rejected(self) -> None:
        set_account_fields("bob", status="disabled")
        with self.assertRaisesRegex(ValueError, "no such user"):
            mail.send(sender="ada", to="bob", subject="x", body="y")

    def test_pad_mail_send_read_delete(self) -> None:
        from crossbar.session import SESSIONS, get_session
        from crossbar.ws import push

        SESSIONS.clear()
        ada = get_session("mail-ada")
        ada.user = "ada"
        ada.host = "grayline"
        ada.phase = "shell"
        bob = get_session("mail-bob")
        bob.user = "bob"
        bob.host = "grayline"
        bob.phase = "shell"
        started = push(ada, "mail send bob hello there\r")
        self.assertIn("Compose to bob", started)
        self.assertEqual(ada.phase, "mail_body")
        done = push(ada, "line one\r.\r")
        self.assertIn("sent ", done)
        self.assertEqual(ada.phase, "mail")
        self.assertIn("/mail>", done)
        listing = push(bob, "mail\r")
        self.assertIn("hello there", listing)
        self.assertIn("1 unread", listing)
        self.assertEqual(bob.phase, "mail")
        mid = mail.list_letters("bob")[0].id
        read = push(bob, f"r {mid}\r")
        self.assertIn("From: ada", read)
        self.assertIn("line one", read)
        self.assertEqual(mail.unread_count("bob"), 0)
        deleted = push(bob, f"d {mid}\r")
        self.assertIn("deleted 1", deleted)
        self.assertIsNone(mail.get_letter("bob", mid))

    def test_reply_forward_archive_and_unread_notice(self) -> None:
        from crossbar.session import SESSIONS, get_session
        from crossbar.ws import push

        SESSIONS.clear()
        mid = mail.send(sender="ada", to="bob", subject="hello", body="hi bob")
        bob = get_session("mail-bob2")
        bob.user = "bob"
        bob.host = "grayline"
        bob.phase = "shell"
        started = push(bob, f"mail reply {mid}\r")
        self.assertIn("Compose to ada", started)
        self.assertEqual(bob.phase, "mail_body")
        self.assertTrue(any(line.startswith(">") for line in bob.mail_body_lines))
        done = push(bob, "thanks\r.\r")
        self.assertIn("sent ", done)
        reply_id = mail.list_letters("ada")[0].id
        reply = mail.get_letter("ada", reply_id)
        assert reply is not None
        self.assertEqual(reply.in_reply_to, mid)
        self.assertTrue(reply.subject.lower().startswith("re:"))

        sent_copy = mail.list_letters("bob", "sent")[0]
        self.assertIn(
            "cannot reply to this letter",
            push(bob, f"mail reply {sent_copy.id}\r"),
        )

        fwd = push(bob, f"mail fwd {mid} carol\r")
        self.assertIn("Compose to carol", fwd)
        push(bob, ".\r")
        carol_letter = mail.list_letters("carol")[0]
        self.assertIsNone(carol_letter.in_reply_to)
        self.assertTrue(carol_letter.subject.lower().startswith("fwd:"))

        unread_mid = mail.send(sender="ada", to="bob", subject="later", body="ping")
        self.assertEqual(mail.unread_count("bob"), 1)
        self.assertTrue(mail.archive("bob", unread_mid))
        self.assertEqual(mail.unread_count("bob"), 0)
        self.assertEqual(len(mail.list_letters("bob", "archive")), 1)

        from crossbar.commands import _arrived

        bob.user = "bob"
        arrived = _arrived(bob)
        self.assertNotIn("new letter", arrived)
        mail.send(sender="ada", to="bob", subject="again", body="yo")
        arrived = _arrived(bob)
        self.assertIn("You have 1 new letter.", arrived)

        mail.broadcast(subject="maint", body="window", handles=["bob"])
        sys_letter = [row for row in mail.list_letters("bob") if row.sender == "sysop"][0]
        self.assertIn(
            "sysop is not accepting mail",
            push(bob, f"mail reply {sys_letter.id}\r"),
        )

    def test_wall_posts_last_ten(self) -> None:
        from crossbar import wall
        from crossbar.session import SESSIONS, get_session
        from crossbar.ws import push

        SESSIONS.clear()
        ada = get_session("wall-ada")
        ada.user = "ada"
        ada.host = "grayline"
        for i in range(12):
            wall.post("ada", f"line {i}")
        shown = push(ada, "wall\r")
        self.assertIn("ada: line 11", shown)
        self.assertIn("ada: line 2", shown)
        self.assertNotIn("ada: line 0", shown)
        self.assertNotIn("ada: line 1\r", shown)
        guest = get_session("wall-guest")
        guest.user = "guest"
        guest.host = "grayline"
        self.assertIn("WALL", push(guest, "wall\r"))
        self.assertIn("logon required", push(guest, "wall nope\r"))


if __name__ == "__main__":
    unittest.main()
