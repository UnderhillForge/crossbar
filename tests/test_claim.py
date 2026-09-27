"""Lobby claim parser. A live node is used only when it answers. No fake accept."""

from __future__ import annotations

import os
import socket
import unittest
from urllib.parse import urlparse

from crossbar.accounts import (
    connect,
    create_account,
    get_account,
    record_claim,
    set_account_fields,
    wallet_of,
)
from crossbar.commands import (
    cmd_chain,
    cmd_claim,
    cmd_leaders,
    cmd_profile,
    cmd_pschain,
    cmd_user_list,
    profile_line,
)
from crossbar import chainrpc
from crossbar import v7
from crossbar.session import Session, get_session


def _sess(name: str) -> Session:
    sess = get_session(f"sid-{name}")
    sess.user = name
    sess.host = "grayline"
    sess.phase = "shell"
    if get_account(name) is None:
        create_account(name, "secret12", f"{name}@example.com")
    set_account_fields(name, wallet="")
    return sess


class ClaimTests(unittest.TestCase):
    def test_missing_link_does_not_call_the_node(self) -> None:
        sess = _sess("nolink")
        called = {"n": 0}

        def boom(method, params):
            called["n"] += 1
            raise AssertionError(method)

        original = chainrpc.call
        chainrpc.call = boom
        try:
            text = cmd_claim(sess, ["DEMO"])
        finally:
            chainrpc.call = original
        self.assertEqual(text, "link a wallet first: ps1 link can be found in profile_config\r\n")
        self.assertEqual(called["n"], 0)
        self.assertEqual(wallet_of("nolink"), "")

    def test_already_yours_does_not_submit(self) -> None:
        sess = _sess("owner")
        ps1 = "ps1" + "ab" * 32
        set_account_fields("owner", wallet=ps1)
        seen: list[str] = []

        def rpc(method, params):
            seen.append(method)
            if method == "listflags" and params.get("address") == ps1:
                return {"flag_ids": ["DEMO"]}
            raise AssertionError(method)

        line, txid = chainrpc.claim_flag("DEMO", ps1, rpc)
        self.assertEqual(line, "already yours")
        self.assertEqual(txid, "")
        self.assertEqual(seen, ["listflags"])
        original = chainrpc.claim_flag
        chainrpc.claim_flag = lambda flag_id, address, rpc=None: ("already yours", "")
        try:
            text = cmd_claim(sess, ["DEMO"])
        finally:
            chainrpc.claim_flag = original
        self.assertEqual(text, "already yours\r\n")

    def test_missing_method_does_not_record(self) -> None:
        sess = _sess("norec")
        ps1 = "ps1" + "cd" * 32
        set_account_fields("norec", wallet=ps1)

        def rpc(method, params):
            raise chainrpc.MethodMissing(method)

        line, txid = chainrpc.claim_flag("DEMO", ps1, rpc)
        self.assertEqual(line, "flags are not on this node yet")
        self.assertEqual(txid, "")
        original = chainrpc.claim_flag
        chainrpc.claim_flag = lambda flag_id, address, rpc=None: ("flags are not on this node yet", "")
        try:
            text = cmd_claim(sess, ["DEMO"])
        finally:
            chainrpc.claim_flag = original
        self.assertIn("not on this node yet", text)
        self.assertNotIn("claimed", text)

    def test_refused_reason_is_one_line(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        ps1 = "ps1" + "ef" * 32
        seed = Ed25519PrivateKey.generate().private_bytes_raw().hex()
        os.environ["PISECURE_AWARDS_KEY"] = seed

        def rpc(method, params):
            if method == "listflags" and "address" in params:
                return {"flag_ids": []}
            if method == "listflags":
                return {"found": True, "bounty_units": 0}
            if method == "listunspent":
                return {"utxos": [{"txid": "aa" * 32, "vout": 0, "units": 5}]}
            if method == "claimflag":
                self.assertEqual(params.get("type"), "claimflag")
                self.assertEqual(params.get("recipient"), ps1)
                self.assertNotIn("answer", params)
                self.assertNotIn("commitment", params)
                paid = [row for row in params.get("outputs") or [] if row.get("address") == ps1]
                self.assertEqual(paid, [])
                return {"status": "rejected", "reason": "flag exhausted"}
            raise AssertionError(method)

        try:
            line, txid = chainrpc.claim_flag("DEMO", ps1, rpc)
        finally:
            os.environ.pop("PISECURE_AWARDS_KEY", None)
        self.assertEqual(line, "flag exhausted")
        self.assertEqual(txid, "")

    def test_profile_rejects_a_duplicate_ps1(self) -> None:
        first = _sess("adaone")
        second = _sess("adatwo")
        ps1 = "ps1" + "11" * 32
        set_account_fields("adaone", wallet=ps1)
        second.phase = "profile_ps1"
        text = profile_line(second, ps1)
        self.assertIn("ps1 already linked", text)
        self.assertEqual(wallet_of("adatwo"), "")
        opened = cmd_profile(first, [])
        self.assertIn("HANDLE  adaone", opened)
        self.assertIn(ps1, opened)
        self.assertNotIn("secret", opened.lower())

    def test_chain_says_when_the_method_is_missing(self) -> None:
        def rpc(method, params):
            raise chainrpc.MethodMissing(method)

        original = chainrpc.call
        chainrpc.call = rpc
        try:
            sess = _sess("chainy")
            text = cmd_chain(sess, [])
        finally:
            chainrpc.call = original
        self.assertEqual(text, "getchaininfo is not on this node yet\r\n")

    def test_bounty_is_escrow_and_the_fee_is_one_coin(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        ps1 = "ps1" + "22" * 32
        seed = Ed25519PrivateKey.generate().private_bytes_raw().hex()
        os.environ["PISECURE_AWARDS_KEY"] = seed
        seen: dict = {}

        def rpc(method, params):
            if method == "listflags" and "address" in params:
                return {"flag_ids": []}
            if method == "listflags":
                return {"found": True, "bounty_units": 10}
            if method == "listunspent":
                return {"utxos": [{"txid": "bb" * 32, "vout": 0, "units": 5}]}
            if method == "claimflag":
                seen["tx"] = params
                return {"status": "rejected", "reason": "flag expired"}
            raise AssertionError(method)

        try:
            line, txid = chainrpc.claim_flag("EVENT", ps1, rpc)
        finally:
            os.environ.pop("PISECURE_AWARDS_KEY", None)
        self.assertEqual(line, "flag expired")
        self.assertEqual(txid, "")
        tx = seen["tx"]
        self.assertEqual(tx["fee"], 1)
        self.assertEqual(tx["inputs"][0]["prev_txid"], "bb" * 32)
        self.assertEqual(tx["outputs"][0], {"address": ps1, "value": 10})
        self.assertEqual(tx["outputs"][1]["value"], 4)
        self.assertNotEqual(tx["outputs"][1]["address"], ps1)
        self.assertNotIn("answer", tx)

    def test_spend_payload_is_the_claim_body(self) -> None:
        tx = {
            "version": 1,
            "fee": 1,
            "type": "claimflag",
            "flag_id": "DEMO",
            "recipient": "ps1" + "ab" * 32,
            "inputs": [{"prev_txid": "aa" * 32, "vout": 0, "public_key": "zz", "signature": "yy"}],
            "outputs": [],
        }
        payload = chainrpc.spend_payload(tx)
        self.assertNotIn("public_key", payload)
        self.assertNotIn("signature", payload)
        self.assertNotIn("answer", payload)
        recipient = "ps1" + "ab" * 32
        expected = (
            '{"fee":1,"flag_id":"DEMO","inputs":[{"prev_txid":"'
            + ("aa" * 32)
            + '","vout":0}],"outputs":[],"recipient":"'
            + recipient
            + '","type":"claimflag","version":1}'
        )
        self.assertEqual(payload, expected)

    def test_repeat_claim_does_not_call_the_node(self) -> None:
        sess = _sess("repeater")
        ps1 = "ps1" + "44" * 32
        set_account_fields("repeater", wallet=ps1)
        record_claim("repeater", "EVENT1", ps1, "aa" * 32)

        def boom(method, params):
            raise AssertionError(method)

        original = chainrpc.call
        chainrpc.call = boom
        try:
            text = cmd_claim(sess, ["EVENT1"])
        finally:
            chainrpc.call = original
        self.assertEqual(text, "already yours\r\n")

    def test_event_flag_from_the_door_submits_a_claim(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        sess = _sess("doorer")
        ps1 = "ps1" + "55" * 32
        set_account_fields("doorer", wallet=ps1)
        sess.host = "bec"
        local = v7._cmd_claim(sess, ["BEC-GUEST"])
        self.assertIn("permission denied", local)
        seed = Ed25519PrivateKey.generate().private_bytes_raw().hex()
        os.environ["PISECURE_AWARDS_KEY"] = seed
        seen: list[str] = []

        def rpc(method, params):
            seen.append(method)
            if method == "listflags" and params.get("address") == ps1:
                return {"flag_ids": []}
            if method == "listflags":
                return {"found": True, "bounty_units": 4, "max_claims": 8}
            if method == "listunspent":
                return {"utxos": [{"txid": "cc" * 32, "vout": 0, "units": 3}]}
            if method == "claimflag":
                self.assertEqual(params.get("flag_id"), "EVENT1")
                self.assertEqual(params.get("recipient"), ps1)
                paid = [row for row in params.get("outputs") or [] if row.get("address") == ps1]
                self.assertEqual(paid, [{"address": ps1, "value": 4}])
                self.assertNotIn("answer", params)
                return {"status": "accepted", "txid": "dd" * 32}
            raise AssertionError(method)

        original = chainrpc.call
        chainrpc.call = rpc
        try:
            text = v7._cmd_claim(sess, ["EVENT1"])
        finally:
            chainrpc.call = original
            os.environ.pop("PISECURE_AWARDS_KEY", None)
        self.assertEqual(text, "claimed\r\n")
        self.assertIn("claimflag", seen)
        self.assertNotIn("createflag", seen)

    def test_pschain_prints_peers_height_hash_and_health(self) -> None:
        secret = "203.0.113.9"

        def rpc(method, params):
            if method == "getblockcount":
                return {"count": 12}
            if method == "getheader":
                self.assertEqual(params, {"height": 12})
                return {"hash": "ab" * 32, "height": 12}
            if method == "getpeers":
                return [
                    {"address": "127.0.0.1", "port": 3144, "host": "pisecure.local"},
                    {"address": secret, "port": 3144, "hint": True},
                ]
            if method == "getthreats":
                return []
            raise AssertionError(method)

        original = chainrpc.call
        chainrpc.call = rpc
        try:
            sess = _sess("pspeek")
            text = cmd_pschain(sess, [])
        finally:
            chainrpc.call = original
        self.assertIn("HEIGHT  12", text)
        self.assertIn("HASH  " + "ab" * 32, text)
        self.assertIn("PEERS  2", text)
        self.assertIn("NODE  pisecure.local  127.0.0.1:3144", text)
        self.assertIn(f"HINT  {secret}:3144", text)
        self.assertIn("HEALTH  UP", text)
        self.assertNotIn("hashrate", text.lower())

    def test_pschain_degraded_hides_threat_details(self) -> None:
        def rpc(method, params):
            if method == "getblockcount":
                return {"count": 3}
            if method == "getheader":
                return {"hash": "cd" * 32}
            if method == "getpeers":
                return []
            if method == "getthreats":
                return [{"ip": "198.51.100.4", "type": "scan"}]
            raise AssertionError(method)

        original = chainrpc.call
        chainrpc.call = rpc
        try:
            text = chainrpc.pschain_text(rpc)
        finally:
            chainrpc.call = original
        self.assertIn("HEALTH  DEGRADED", text)
        self.assertIn("PEERS  0", text)
        self.assertNotIn("198.51.100.4", text)
        self.assertNotIn("scan", text)

    def test_pschain_when_the_node_is_down(self) -> None:
        def rpc(method, params):
            raise chainrpc.RpcDown("closed")

        text = chainrpc.pschain_text(rpc)
        self.assertEqual(text, "node did not answer\r\n")

    def test_leaders_and_user_list(self) -> None:
        import tempfile

        previous = os.environ.get("CROSSBAR_DB")
        fd, path = tempfile.mkstemp(prefix="grayline-board-", suffix=".db")
        os.close(fd)
        os.environ["CROSSBAR_DB"] = path
        try:
            conn = connect()
            now = "2026-09-01T00:00:00Z"
            conn.execute(
                "INSERT INTO accounts (handle, password_hash, email, created, last_login, status) "
                "VALUES (?, ?, ?, ?, ?, 'ok')",
                ("quiet", "x", "quiet@example.com", now, "2026-09-02T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO accounts (handle, password_hash, email, created, last_login, status) "
                "VALUES (?, ?, ?, ?, ?, 'ok')",
                ("ace", "x", "ace@example.com", now, "2026-09-03T00:00:00Z"),
            )
            for index in range(21):
                handle = f"b{index:02d}"
                conn.execute(
                    "INSERT INTO accounts (handle, password_hash, email, created, last_login, status) "
                    "VALUES (?, ?, ?, ?, ?, 'ok')",
                    (handle, "x", f"{handle}@example.com", now, now),
                )
            conn.commit()
            for n in range(3):
                record_claim("ace", f"F{n}", "ps1" + "66" * 32, f"{n:064x}")
            for index in range(21):
                record_claim(f"b{index:02d}", "ONE", "ps1" + "77" * 32, f"{index + 1:064x}")
            sess = get_session("sid-board")
            sess.user = "ace"
            sess.host = "grayline"
            board = cmd_leaders(sess, [])
            listing = cmd_user_list(sess, [])
        finally:
            if previous is None:
                os.environ.pop("CROSSBAR_DB", None)
            else:
                os.environ["CROSSBAR_DB"] = previous
            connect()
            os.remove(path)
        self.assertTrue(board.startswith("RANK  HANDLE        CLAIMS  LAST"))
        self.assertIn("1     ace", board)
        self.assertLess(board.find("ace"), board.find("b00"))
        lines = [line for line in board.splitlines()[1:] if line]
        self.assertEqual(len(lines), 20)
        self.assertNotIn("b19", board)
        self.assertNotIn("b20", board)
        self.assertIn("quiet         -     2026-09-02T00:00:00Z", listing)
        self.assertNotIn("@", listing)
        self.assertNotIn("ps1", listing)

    def test_live_node_only_if_it_answers(self) -> None:
        url = urlparse(chainrpc.rpc_url())
        host = url.hostname or "127.0.0.1"
        port = url.port or 3144
        sock = socket.socket()
        sock.settimeout(0.4)
        try:
            up = sock.connect_ex((host, port)) == 0
        finally:
            sock.close()
        if not up:
            return
        text = chainrpc.chain_text()
        self.assertTrue(
            text.startswith("HEIGHT") or "getchaininfo is not on this node yet" in text or "node did not answer" in text
        )
        self.assertNotIn("claimed", text)


if __name__ == "__main__":
    unittest.main()
