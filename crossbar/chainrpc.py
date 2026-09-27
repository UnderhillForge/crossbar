"""Server-side PiSecure JSON-RPC. The browser never opens this socket.

The awards seed stays in PISECURE_AWARDS_KEY. A player account stores one ps1
address and never a spending key. Claim bodies are flag_id plus that address.
Door answers are not sent.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

Rpc = Callable[[str, Any], Any]

_URL = "ws://127.0.0.1:3144"
_NODE_LINES = (
    "unknown flag",
    "already claimed",
    "flag exhausted",
    "flag expired",
    "unlimited flag cannot pay",
)


class RpcDown(Exception):
    pass


class MethodMissing(Exception):
    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


def rpc_url() -> str:
    return os.environ.get("PISECURE_RPC_URL", "").strip() or _URL


def _seed() -> bytes | None:
    raw = os.environ.get("PISECURE_AWARDS_KEY", "").strip().lower()
    if len(raw) != 64:
        return None
    try:
        seed = bytes.fromhex(raw)
    except ValueError:
        return None
    return seed if len(seed) == 32 else None


def awards_address() -> str | None:
    seed = _seed()
    if seed is None:
        return None
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ps1" + hashlib.sha256(pub).hexdigest()


def _sign(message: str) -> tuple[str, str] | None:
    seed = _seed()
    if seed is None:
        return None
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    sig = key.sign(message.encode("utf-8")).hex()
    return pub, sig


def spend_payload(tx: dict) -> str:
    """Bytes pswallet signs. nlohmann::json sorts keys and adds no spaces."""
    inputs = [{"prev_txid": item["prev_txid"], "vout": item["vout"]} for item in tx["inputs"]]
    outputs = [{"address": item["address"], "value": item["value"]} for item in tx["outputs"]]
    body: dict[str, Any] = {
        "fee": tx["fee"],
        "inputs": inputs,
        "outputs": outputs,
        "version": tx["version"],
    }
    kind = tx.get("type") or ""
    if kind == "claimflag":
        body["type"] = "claimflag"
        body["flag_id"] = tx["flag_id"]
        body["recipient"] = tx["recipient"]
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def normalize_ps1(text: str) -> str:
    raw = text.strip()
    if len(raw) != 67 or raw[:3].lower() != "ps1":
        return ""
    hexpart = raw[3:]
    if len(hexpart) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in hexpart):
        return ""
    return "ps1" + hexpart.lower()


def call(method: str, params: Any) -> Any:
    """One JSON-RPC call. Tests replace this."""
    import websockets.sync.client

    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        with websockets.sync.client.connect(rpc_url(), open_timeout=2, close_timeout=1) as sock:
            sock.send(json.dumps(request))
            raw = sock.recv(timeout=5)
    except Exception as exc:
        raise RpcDown(str(exc)) from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RpcDown("bad rpc") from exc
    if not isinstance(parsed, dict):
        raise RpcDown("bad rpc")
    error = parsed.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message") or "")
        if code == -32601 or "method not found" in message.lower():
            raise MethodMissing(method)
        raise RpcDown(message or "rpc error")
    return parsed.get("result")


def node_line(reason: str) -> str:
    text = reason.strip().lower()
    for line in _NODE_LINES:
        if line in text:
            return line
    return "claim refused"


def _coins(listed: Any) -> list[dict]:
    if not isinstance(listed, dict):
        return []
    rows = listed.get("utxos") or listed.get("unspent") or []
    if not isinstance(rows, list):
        return []
    coins = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        txid = str(row.get("txid") or "")
        if not txid:
            continue
        coins.append(
            {
                "prev_txid": txid,
                "vout": int(row.get("vout") or 0),
                "units": int(row.get("units") or 0),
            }
        )
    return coins


def build_claim(flag_id: str, recipient: str, listed: Any, bounty: int) -> dict | str:
    """Return a signed claimflag tx, or a one-line error. Never marks a claim."""
    awards = awards_address()
    if awards is None:
        return "awards key is not set"
    # The fee comes from the awards wallet. A bounty is flag escrow, not another input.
    fee = 1
    chosen = []
    total = 0
    coins = sorted(_coins(listed), key=lambda coin: coin["units"], reverse=True)
    for coin in coins:
        if total >= fee or len(chosen) >= 200:
            break
        chosen.append(coin)
        total += coin["units"]
    if total < fee:
        return "awards wallet cannot pay the fee"
    outputs = []
    if bounty > 0:
        outputs.append({"address": recipient, "value": bounty})
    change = total - fee
    if change > 0:
        outputs.append({"address": awards, "value": change})
    tx = {
        "version": 1,
        "fee": 1,
        "type": "claimflag",
        "flag_id": flag_id,
        "recipient": recipient,
        "inputs": [{"prev_txid": coin["prev_txid"], "vout": coin["vout"]} for coin in chosen],
        "outputs": outputs,
    }
    signed = _sign(spend_payload(tx))
    if signed is None:
        return "awards key is not set"
    pub, sig = signed
    for item in tx["inputs"]:
        item["public_key"] = pub
        item["signature"] = sig
    return tx


def claim_flag(flag_id: str, ps1: str, rpc: Rpc | None = None) -> tuple[str, str]:
    """Return (line, txid). txid is empty unless the node accepted the claim."""
    remote = rpc or call
    try:
        owned = remote("listflags", {"address": ps1})
    except MethodMissing:
        return "flags are not on this node yet", ""
    except RpcDown:
        return "node did not answer", ""
    ids = []
    if isinstance(owned, dict):
        raw_ids = owned.get("flag_ids") or []
        if isinstance(raw_ids, list):
            ids = [str(item) for item in raw_ids]
    if flag_id in ids:
        return "already yours", ""
    try:
        detail = remote("listflags", {"flag_id": flag_id})
    except MethodMissing:
        return "flags are not on this node yet", ""
    except RpcDown:
        return "node did not answer", ""
    if not isinstance(detail, dict) or not detail.get("found", False):
        return "unknown flag", ""
    bounty = int(detail.get("bounty_units") or 0)
    try:
        listed = remote("listunspent", {"address": awards_address() or ""})
    except MethodMissing:
        return "flags are not on this node yet", ""
    except RpcDown:
        return "node did not answer", ""
    tx = build_claim(flag_id, ps1, listed, bounty)
    if isinstance(tx, str):
        return tx, ""
    try:
        result = remote("claimflag", tx)
    except MethodMissing:
        return "flags are not on this node yet", ""
    except RpcDown:
        return "node did not answer", ""
    if not isinstance(result, dict):
        return "claim refused", ""
    if str(result.get("status") or "") == "accepted":
        return "claimed", str(result.get("txid") or "")
    return node_line(str(result.get("reason") or "")), ""


def chain_text(rpc: Rpc | None = None) -> str:
    remote = rpc or call
    try:
        info = remote("getchaininfo", {})
    except MethodMissing:
        return "getchaininfo is not on this node yet\r\n"
    except RpcDown:
        return "node did not answer\r\n"
    if not isinstance(info, dict):
        return "getchaininfo is not on this node yet\r\n"
    height = info.get("height", info.get("blocks", info.get("count")))
    tip = info.get("tip") or info.get("hash") or info.get("bestblockhash") or ""
    difficulty = info.get("difficulty", info.get("bits"))
    supply = info.get("supply", info.get("circulating"))
    lines = []
    if height is not None:
        lines.append(f"HEIGHT  {height}")
    if tip:
        lines.append(f"TIP  {tip}")
    if difficulty is not None:
        lines.append(f"DIFFICULTY  {difficulty}")
    if supply is not None:
        lines.append(f"SUPPLY  {supply}")
    if not lines:
        return "getchaininfo is not on this node yet\r\n"
    return "\r\n".join(lines) + "\r\n"


def lookup_name(name: str, rpc: Rpc | None = None) -> str:
    remote = rpc or call
    found = remote("namelookup", {"name": name})
    if isinstance(found, dict) and found.get("found") and found.get("address"):
        return normalize_ps1(str(found["address"]))
    return ""


def balance_line(ps1: str, rpc: Rpc | None = None) -> str:
    remote = rpc or call
    try:
        listed = remote("listunspent", {"address": ps1})
    except (MethodMissing, RpcDown):
        return ""
    if not isinstance(listed, dict):
        return ""
    amount = listed.get("amount")
    if amount is None:
        return ""
    return f"BALANCE  {amount}\r\n"
