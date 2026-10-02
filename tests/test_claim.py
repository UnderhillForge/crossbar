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



def pad_prompt_end(user: str = "ada", path: str = "main") -> str:
    return f"{user}@grayline/{path}> "


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
        self.assertEqual(first.phase, "profile")

    def test_profile_menu_asks_instead_of_returning_to_gl(self) -> None:
        from crossbar.accounts import email_of
        from crossbar.ws import push

        sess = _sess("editor")
        sess.phase = "shell"
        opened = push(sess, "profile_config\r")
        self.assertIn("1 EMAIL", opened)
        self.assertTrue(opened.endswith(pad_prompt_end("editor", "profile")))
        asked = push(sess, "1\r")
        self.assertEqual(sess.phase, "profile_email")
        self.assertTrue(asked.endswith(pad_prompt_end("editor", "profile") + "EMAIL: "))
        self.assertNotIn("login:", asked)
        saved = push(sess, "editor@example.com\r")
        self.assertIn("email saved", saved)
        self.assertIn("editor@example.com", saved)
        self.assertTrue(saved.endswith(pad_prompt_end("editor", "profile")))
        self.assertEqual(email_of("editor"), "editor@example.com")
        secret = push(sess, "2\rsecret99\rsecret99\r")
        self.assertNotIn("secret99", secret)
        self.assertIn("password saved", secret)
        self.assertTrue(secret.endswith(pad_prompt_end("editor", "profile")))
        linked = push(sess, "3\r")
        self.assertTrue(linked.endswith(pad_prompt_end("editor", "profile") + "PS1: "))
        menu = push(sess, "\r")
        self.assertTrue(menu.endswith(pad_prompt_end("editor", "profile")))
        self.assertEqual(sess.phase, "profile")
        back = push(sess, "q\r")
        self.assertTrue(back.endswith(pad_prompt_end("editor")))
        self.assertEqual(sess.phase, "shell")
        push(sess, "profile_config\r")
        cancelled = push(sess, "\x03")
        self.assertTrue(cancelled.endswith(pad_prompt_end("editor")))
        self.assertEqual(sess.phase, "shell")

    def test_chain_is_not_a_lobby_command(self) -> None:
        from crossbar.ws import push

        sess = _sess("nochains")
        sess.phase = "shell"
        text = push(sess, "chain\r")
        self.assertIn("chain: not found", text)

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
                return {"hash": "ab" * 32, "height": 12, "difficulty": 9}
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
        local_tag = chainrpc._peer_tag(
            {"address": "127.0.0.1", "port": 3144, "host": "pisecure.local"}
        )
        hint_tag = chainrpc._peer_tag({"address": secret, "port": 3144, "hint": True})
        self.assertIn("HEIGHT  12", text)
        self.assertIn("DIFFICULTY  9", text)
        self.assertIn("HASH  " + "ab" * 32, text)
        self.assertIn("NODES  2", text)
        self.assertIn(f"NODE  {local_tag}", text)
        self.assertIn(f"HINT  {hint_tag}", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("3144", text)
        self.assertNotIn("pisecure.local", text)
        self.assertIn("HEALTH  UP", text)
        self.assertIn("HASHRATE  unavailable", text)

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
        self.assertIn("NODES  0", text)
        self.assertNotIn("198.51.100.4", text)
        self.assertNotIn("scan", text)

    def test_pschain_when_the_node_is_down(self) -> None:
        def rpc(method, params):
            raise chainrpc.RpcDown("closed")

        http = chainrpc._http_json
        chainrpc._http_json = lambda path: None
        try:
            text = chainrpc.pschain_text(rpc)
        finally:
            chainrpc._http_json = http
        self.assertEqual(text, "node did not answer\r\n")

    def test_bootstrap_feeds_pschain_when_the_node_is_down(self) -> None:
        def rpc(method, params):
            raise chainrpc.RpcDown("closed")

        def http(path):
            if path == "/api/v1/network/live":
                return {
                    "height": 1171,
                    "tip": "ab" * 32,
                    "difficulty": 16,
                    "network_hashrate": None,
                    "health_status": "Poor",
                    "health_score": 15,
                }
            if path == "/api/v1/nodes/list":
                return {
                    "active_nodes_count": 1,
                    "nodes": [
                        {
                            "node_id": "pisecured-lab",
                            "p2p_host": "203.0.113.10",
                            "rpc_port": 3144,
                            "status": "active",
                        }
                    ],
                }
            return None

        original = chainrpc.call
        http_orig = chainrpc._http_json
        chainrpc.call = rpc
        chainrpc._http_json = http
        try:
            board = chainrpc.pschain_text()
        finally:
            chainrpc.call = original
            chainrpc._http_json = http_orig
        lab_tag = chainrpc._peer_tag(
            {
                "node_id": "pisecured-lab",
                "p2p_host": "203.0.113.10",
                "rpc_port": 3144,
                "status": "active",
            }
        )
        self.assertIn("HEIGHT  1171", board)
        self.assertIn("DIFFICULTY  16", board)
        self.assertIn("HASH  " + "ab" * 32, board)
        self.assertIn("HASHRATE  unavailable", board)
        self.assertIn("HEALTH  Poor  15", board)
        self.assertIn("NODES  1", board)
        self.assertIn(f"NODE  {lab_tag}  active", board)
        self.assertNotIn("203.0.113.10", board)
        self.assertNotIn("3144", board)
        self.assertNotIn("pisecured-lab", board)

    def test_profile_stores_email_password_and_ps1(self) -> None:
        from crossbar.accounts import authenticate, email_of
        from crossbar.ws import push

        sess = _sess("keeper")
        sess.phase = "shell"
        push(sess, "profile_config\r")
        saved = push(sess, "keeper@example.com\r")
        self.assertIn("email saved", saved)
        self.assertIn("EMAIL   keeper@example.com", saved)
        self.assertEqual(email_of("keeper"), "keeper@example.com")
        push(sess, "2\rsecret99\r")
        done = push(sess, "secret99\r")
        self.assertIn("password saved", done)
        self.assertTrue(authenticate("keeper", "secret99"))
        ps1 = "ps1" + "99" * 32
        linked = push(sess, ps1 + "\r")
        self.assertIn("ps1 saved", linked)
        self.assertEqual(wallet_of("keeper"), ps1)
        self.assertIn("keeper@example.com", linked)

    def test_short_name_resolves_to_a_ps1(self) -> None:
        from crossbar.ws import push

        sess = _sess("namer")
        sess.phase = "shell"
        ps1 = "ps1" + "a1" * 32
        seen: list[str] = []

        def rpc(method, params):
            seen.append(method)
            if method == "namelookup" and params.get("name") == "alice":
                return {"found": True, "name": "alice", "address": ps1}
            raise chainrpc.RpcDown("closed")

        original = chainrpc.call
        chainrpc.call = rpc
        try:
            push(sess, "profile_config\r")
            saved = push(sess, "alice\r")
        finally:
            chainrpc.call = original
        self.assertIn("ps1 saved", saved)
        self.assertIn(ps1, saved)
        self.assertEqual(wallet_of("namer"), ps1)
        self.assertIn("namelookup", seen)

    def test_short_name_uses_a_directory_node_when_the_local_node_is_down(self) -> None:
        ps1 = "ps1" + "a2" * 32

        def rpc(method, params):
            raise chainrpc.RpcDown("closed")

        def http(path):
            if path == "/api/v1/nodes/list":
                return {"nodes": [{"p2p_host": "203.0.113.10", "rpc_port": 3144}]}
            return None

        def at(url, method, params):
            self.assertEqual(url, "ws://203.0.113.10:3144")
            self.assertEqual(method, "namelookup")
            self.assertEqual(params, {"name": "alice"})
            return {"found": True, "name": "alice", "address": ps1}

        original = chainrpc.call
        http_orig = chainrpc._http_json
        at_orig = chainrpc.call_at
        chainrpc.call = rpc
        chainrpc._http_json = http
        chainrpc.call_at = at
        try:
            found = chainrpc.lookup_name("alice")
        finally:
            chainrpc.call = original
            chainrpc._http_json = http_orig
            chainrpc.call_at = at_orig
        self.assertEqual(found, ps1)

    def test_short_name_is_kept_when_no_node_answers(self) -> None:
        from crossbar.ws import push

        sess = _sess("alias")
        sess.phase = "shell"

        def rpc(method, params):
            raise chainrpc.RpcDown("closed")

        original = chainrpc.call
        http = chainrpc._http_json
        chainrpc.call = rpc
        chainrpc._http_json = lambda path: None
        try:
            push(sess, "profile_config\r")
            saved = push(sess, "alice\r")
        finally:
            chainrpc.call = original
            chainrpc._http_json = http
        self.assertIn("name saved", saved)
        self.assertIn("PS1     alice", saved)
        self.assertEqual(wallet_of("alias"), "alice")

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
        text = chainrpc.pschain_text()
        self.assertTrue("HEIGHT" in text or "node did not answer" in text)
        self.assertNotIn("claimed", text)


if __name__ == "__main__":
    unittest.main()
