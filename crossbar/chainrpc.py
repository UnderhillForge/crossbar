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
_BOOTSTRAP = "https://pisecure-bootstrap-production.up.railway.app"
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


def bootstrap_url() -> str:
    return os.environ.get("PISECURE_BOOTSTRAP_URL", "").strip() or _BOOTSTRAP


def _http_json(path: str) -> Any:
    """JSON from the public bootstrap. None when that service does not answer."""
    import httpx

    url = bootstrap_url().rstrip("/") + path
    try:
        response = httpx.get(url, timeout=5.0, headers={"User-Agent": "crossbar"})
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, (dict, list)) else None


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


def valid_shortname(name: str) -> bool:
    text = name.strip()
    if not 2 <= len(text) <= 32 or not text[0].isalpha():
        return False
    return all(ch.isalnum() or ch in "_-" for ch in text)


def call_at(url: str, method: str, params: Any) -> Any:
    """One JSON-RPC call to a specific node. Tests replace this."""
    import websockets.sync.client

    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        with websockets.sync.client.connect(url, open_timeout=2, close_timeout=1) as sock:
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


def call(method: str, params: Any) -> Any:
    """JSON-RPC on PISECURE_RPC_URL. Tests replace this."""
    return call_at(rpc_url(), method, params)


def directory_rpc_urls() -> list[str]:
    """Websocket URLs from the bootstrap node list, configured node first."""
    urls = [rpc_url()]
    listed = _http_json("/api/v1/nodes/list")
    nodes = listed.get("nodes") if isinstance(listed, dict) else None
    if isinstance(nodes, list):
        for row in nodes:
            if not isinstance(row, dict):
                continue
            host = str(row.get("p2p_host") or row.get("host") or "").strip()
            if not host:
                continue
            port = row.get("rpc_port") or 3144
            urls.append(f"ws://{host}:{port}")
    seen: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.append(url)
    return seen


def _ps1_from_lookup(found: Any) -> str:
    if not isinstance(found, dict) or not found.get("found"):
        return ""
    return normalize_ps1(str(found.get("address") or ""))


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


def _peer_rows(result: Any) -> list[dict]:
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if not isinstance(result, dict):
        return []
    rows: list[dict] = []
    self_row = result.get("self")
    if isinstance(self_row, dict):
        rows.append(self_row)
    nested = result.get("peers") or result.get("nodes")
    if isinstance(nested, list):
        rows.extend(row for row in nested if isinstance(row, dict))
    if rows:
        return rows
    if result.get("address") or result.get("host") or result.get("peer_id") or result.get("node_id"):
        return [result]
    return []


def _peer_identity(row: dict) -> str:
    """Stable id to hash. Prefer peer_id / node_id / host; address:port is last resort."""
    for key in ("peer_id", "node_id", "id", "host"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    address = str(row.get("address") or row.get("p2p_host") or "").strip()
    port = row.get("port", row.get("rpc_port", row.get("p2p_port", "")))
    if address and port not in (None, ""):
        return f"{address}:{port}"
    return address or "unknown"


def _peer_tag(row: dict) -> str:
    """Short hash of the peer identity. Never the raw ip:port."""
    digest = hashlib.sha256(_peer_identity(row).encode("utf-8")).hexdigest()
    return digest[:12]


def _peer_line(row: dict) -> str:
    kind = "HINT" if row.get("hint") else "NODE"
    return f"  {kind}  {_peer_tag(row)}"


def _bootstrap_pschain() -> str:
    live = _http_json("/api/v1/network/live")
    listed = _http_json("/api/v1/nodes/list")
    if not isinstance(live, dict) and not isinstance(listed, dict):
        return ""
    live = live if isinstance(live, dict) else {}
    lines = []
    height = live.get("height")
    lines.append(f"HEIGHT  {height}" if height is not None else "HEIGHT  unavailable")
    difficulty = live.get("difficulty")
    lines.append(f"DIFFICULTY  {difficulty}" if difficulty is not None else "DIFFICULTY  unavailable")
    tip = str(live.get("tip") or "")
    lines.append(f"HASH  {tip}" if tip else "HASH  unavailable")
    rate = live.get("network_hashrate")
    lines.append(f"HASHRATE  {rate}" if rate not in (None, "") else "HASHRATE  unavailable")
    status = str(live.get("health_status") or "").strip()
    score = live.get("health_score")
    if status and score is not None:
        lines.append(f"HEALTH  {status}  {score}")
    elif status:
        lines.append(f"HEALTH  {status}")
    elif score is not None:
        lines.append(f"HEALTH  {score}")
    else:
        lines.append("HEALTH  unavailable")
    nodes = listed.get("nodes") if isinstance(listed, dict) else None
    if not isinstance(nodes, list):
        nodes = []
    active = listed.get("active_nodes_count") if isinstance(listed, dict) else None
    if active is None:
        active = live.get("active_nodes", len(nodes))
    lines.append(f"NODES  {active}")
    for row in nodes:
        if not isinstance(row, dict):
            continue
        state = str(row.get("status") or "").strip()
        line = f"  NODE  {_peer_tag(row)}"
        if state:
            line = f"{line}  {state}"
        lines.append(line)
    return "\r\n".join(lines) + "\r\n"


def pschain_text(rpc: Rpc | None = None) -> str:
    """Nodes, height, tip hash, hashrate, and health.

    A pisecured node answers first. The bootstrap directory is used when it does not.
    A missing hashrate is left unavailable.
    """
    remote = rpc or call
    try:
        counted = remote("getblockcount", {})
    except (MethodMissing, RpcDown):
        counted = None
    if isinstance(counted, dict) and counted.get("count") is not None:
        height = counted.get("count")
        lines = [f"HEIGHT  {height}"]
        tip = ""
        difficulty = None
        try:
            header = remote("getheader", {"height": height})
        except (MethodMissing, RpcDown):
            header = None
        if isinstance(header, dict):
            tip = str(header.get("hash") or "")
            difficulty = header.get("difficulty")
        lines.append(f"DIFFICULTY  {difficulty}" if difficulty is not None else "DIFFICULTY  unavailable")
        lines.append(f"HASH  {tip}" if tip else "HASH  unavailable")
        rate = counted.get("network_hashrate")
        lines.append(f"HASHRATE  {rate}" if rate not in (None, "") else "HASHRATE  unavailable")
        try:
            peers = remote("getpeers", {})
        except (MethodMissing, RpcDown):
            peers = None
        if peers is not None:
            shown = [_peer_line(row) for row in _peer_rows(peers)]
            lines.append(f"NODES  {len(shown)}")
            lines.extend(shown)
        else:
            lines.append("NODES  unavailable")
        health = "UP"
        try:
            threats = remote("getthreats", [])
        except (MethodMissing, RpcDown):
            threats = None
        if isinstance(threats, list) and threats:
            health = "DEGRADED"
        lines.append(f"HEALTH  {health}")
        return "\r\n".join(lines) + "\r\n"
    text = _bootstrap_pschain()
    if text:
        return text
    return "node did not answer\r\n"


def lookup_name(name: str, rpc: Rpc | None = None) -> str:
    """Resolve a short name to a ps1. The configured node is first, then the directory."""
    text = name.strip()
    if not valid_shortname(text):
        return ""
    remote = rpc or call
    try:
        found = remote("namelookup", {"name": text})
    except (MethodMissing, RpcDown):
        found = None
    else:
        ps1 = _ps1_from_lookup(found)
        if ps1:
            return ps1
        if isinstance(found, dict):
            return ""
    if rpc is not None:
        raise RpcDown("node did not answer")
    answered = False
    for url in directory_rpc_urls():
        if url == rpc_url():
            continue
        try:
            found = call_at(url, "namelookup", {"name": text})
        except (MethodMissing, RpcDown):
            continue
        answered = True
        ps1 = _ps1_from_lookup(found)
        if ps1:
            return ps1
    if not answered:
        raise RpcDown("node did not answer")
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
