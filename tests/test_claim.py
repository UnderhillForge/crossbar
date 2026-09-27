"""Lobby claim parser. A live node is used only when it answers. No fake accept."""

from __future__ import annotations

import os
import socket
import unittest
from urllib.parse import urlparse

from crossbar.accounts import create_account, get_account, set_account_fields, wallet_of
from crossbar.commands import cmd_chain, cmd_claim, cmd_profile, profile_line
from crossbar import chainrpc
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
