"""Virtual UNIX shell. Packs supply catalog.yaml and tree/. Nothing here is exec'd.

A later flavor (linux31) is another pack directory. See packs/FLAVORS.md.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import yaml

from crossbar.accounts import achievements_for, get_account, grant_achievement

from crossbar.session import Session, pop_hop, push_hop

_REPO = Path(__file__).resolve().parent.parent
_GOLD: dict[str, dict[str, dict]] = {}
_CATALOG: dict[str, dict] = {}

_REFUSED = frozenset(
    {
        "python",
        "python3",
        "perl",
        "gcc",
        "cc",
        "curl",
        "wget",
        "ssh",
        "nmap",
        "bash",
        "apt",
        "apt-get",
        "docker",
        "sudo",
        "nc",
        "netcat",
        "systemctl",
    }
)
_ZONE = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(_ZONE)


def _pack_dir(host: str) -> Path:
    from crossbar.packs import get_pack

    pack = get_pack(host)
    folder = (pack.data or "big-evil").strip() or "big-evil"
    return _REPO / "packs" / folder


def doors_text() -> str:
    return (_pack_dir("bec") / "doors.txt").read_text(encoding="utf-8").replace("\n", "\r\n")


def _empty_state() -> dict:
    return {"users": {}, "meta": {}, "files": {}, "deleted": []}


def _registered(sess: Session) -> str | None:
    handle = (sess.user or "").strip()
    if not handle or handle.lower() == "guest":
        return None
    if get_account(handle) is None:
        return None
    return handle


def _overlay_path(handle: str) -> Path:
    override = os.environ.get("BEC_STATE", "").strip()
    if override:
        return Path(override)
    safe = re.sub(r"[^A-Za-z0-9._-]", "", handle) or "user"
    return _REPO / "data" / "users" / safe / "bec" / "state.json"


def _load_state(sess: Session) -> dict:
    handle = _registered(sess)
    if handle is None:
        store = sess.v7_store
        if not isinstance(store, dict) or "files" not in store:
            sess.v7_store = _empty_state()
        return sess.v7_store
    path = _overlay_path(handle)
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("users", {})
    data.setdefault("meta", {})
    data.setdefault("files", {})
    data.setdefault("deleted", [])
    return data


def _save_state(sess: Session, state: dict) -> None:
    handle = _registered(sess)
    if handle is None:
        sess.v7_store = state
        return
    path = _overlay_path(handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _catalog(host: str) -> dict:
    root = str(_pack_dir(host))
    if root not in _CATALOG:
        path = Path(root) / "catalog.yaml"
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        _CATALOG[root] = loaded or {}
    return _CATALOG[root]


def _dir_node(mode: str, uid: int, gid: int, mtime: str) -> dict:
    return {
        "type": "dir",
        "dir": True,
        "mode": str(mode),
        "uid": int(uid),
        "gid": int(gid),
        "mtime": mtime,
        "size": 512,
        "nlink": 2,
    }


def _ensure_dir(nodes: dict[str, dict], path: str) -> None:
    if path in nodes:
        return
    if path != "/":
        _ensure_dir(nodes, path.rsplit("/", 1)[0] or "/")
    if path not in nodes:
        nodes[path] = _dir_node("755", 0, 0, "Jun 18  1987")


def _exec_size(name: str, sizes: dict) -> int:
    if sizes.get(name) is not None:
        return int(sizes[name])
    return min(8192, 512 * (2 + (sum(name.encode()) % 14)))


def _add_exec(nodes: dict[str, dict], directory: str, name: str, block: dict, sizes: dict) -> None:
    _ensure_dir(nodes, directory)
    nodes[f"{directory}/{name}"] = {
        "type": "exec",
        "dir": False,
        "mode": str(block.get("mode", "755")),
        "uid": int(block.get("uid", 2)),
        "gid": int(block.get("gid", 2)),
        "size": _exec_size(name, sizes),
        "mtime": str(block.get("mtime", "Jan 12  1986")),
        "nlink": 1,
    }


def _mount_execs(nodes: dict[str, dict], directory: str, block: dict) -> None:
    if not block:
        return
    _ensure_dir(nodes, directory)
    nodes[directory]["mode"] = "755"
    nodes[directory]["uid"] = int(block.get("uid", nodes[directory].get("uid", 0)))
    nodes[directory]["gid"] = int(block.get("gid", nodes[directory].get("gid", 0)))
    nodes[directory]["mtime"] = str(block.get("mtime", nodes[directory].get("mtime")))
    sizes = block.get("sizes") or {}
    names = [str(name) for name in list(block.get("names") or []) + list(block.get("stubs") or [])]
    for name in names:
        _add_exec(nodes, directory, name, block, sizes)


def _apply_manifest(nodes: dict[str, dict], table: dict, tree: Path) -> None:
    for path, info in (table or {}).items():
        if not isinstance(info, dict) or not str(path).startswith("/"):
            continue
        if nodes.get(path, {}).get("type") == "exec":
            continue
        parent = path if info.get("dir") else (path.rsplit("/", 1)[0] or "/")
        _ensure_dir(nodes, parent)
        if info.get("dir"):
            node = nodes.get(path) or _dir_node("755", 0, 0, "Jun 18  1987")
            node["type"] = "dir"
            node["dir"] = True
            node["mode"] = str(info.get("mode", node.get("mode", "755")))
            node["uid"] = int(info.get("uid", node.get("uid", 0)))
            node["gid"] = int(info.get("gid", node.get("gid", 0)))
            if info.get("mtime"):
                node["mtime"] = str(info["mtime"])
            node.setdefault("size", 512)
            nodes[path] = node
            continue
        rel = info.get("file")
        if not rel:
            continue
        disk = tree / rel
        nodes[path] = {
            "type": "file",
            "dir": False,
            "mode": str(info.get("mode", "644")),
            "uid": int(info.get("uid", 0)),
            "gid": int(info.get("gid", 0)),
            "mtime": str(info.get("mtime") or "Jun 18  1987"),
            "file": rel,
            "size": disk.stat().st_size if disk.is_file() else 0,
            "nlink": 1,
        }


def _finish_links(nodes: dict[str, dict]) -> None:
    for path, info in nodes.items():
        if info.get("type") != "dir":
            info["nlink"] = 1
            continue
        kids = 0
        for other, other_info in nodes.items():
            if other == path or other_info.get("type") != "dir":
                continue
            parent = other.rsplit("/", 1)[0] or "/"
            if parent == path:
                kids += 1
        info["nlink"] = 2 + kids


def _build_gold(host: str) -> dict[str, dict]:
    root = _pack_dir(host)
    manifest_path = root / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if host == "bec-mf":
        nodes: dict[str, dict] = {"/": _dir_node("755", 0, 0, "Sep 25  1985")}
        _apply_manifest(nodes, (manifest or {}).get("mf") or {}, root / "mf-tree")
        _finish_links(nodes)
        return nodes
    catalog = _catalog(host)
    nodes = {"/": _dir_node("755", 0, 0, "Sep 25  1985")}
    for item in catalog.get("dirs") or []:
        path = str(item.get("path") or "")
        if not path.startswith("/"):
            continue
        _ensure_dir(nodes, path)
        nodes[path] = _dir_node(
            str(item.get("mode", "755")),
            int(item.get("uid", 0)),
            int(item.get("gid", 0)),
            str(item.get("mtime", "Jun 18  1987")),
        )
    unix = catalog.get("unix")
    if isinstance(unix, dict):
        nodes["/unix"] = {
            "type": "exec",
            "dir": False,
            "mode": str(unix.get("mode", "755")),
            "uid": int(unix.get("uid", 0)),
            "gid": int(unix.get("gid", 0)),
            "size": int(unix.get("size", 40960)),
            "mtime": str(unix.get("mtime", "Sep 25  1985")),
            "nlink": 1,
        }
    _mount_execs(nodes, "/bin", catalog.get("bin") or {})
    _mount_execs(nodes, "/usr/bin", catalog.get("usrbin") or {})
    for item in catalog.get("localbin") or []:
        name = str(item["name"])
        sizes = {name: item.get("size")} if item.get("size") is not None else {}
        _add_exec(nodes, "/usr/local/bin", name, item, sizes)
    _ensure_dir(nodes, "/dev")
    for item in catalog.get("devs") or []:
        name = str(item["name"])
        nodes[f"/dev/{name}"] = {
            "type": "dev",
            "dir": False,
            "kind": str(item.get("kind", "c")),
            "major": int(item.get("major", 0)),
            "minor": int(item.get("minor", 0)),
            "mode": str(item.get("mode", "666")),
            "uid": int(item.get("uid", 0)),
            "gid": int(item.get("gid", 0)),
            "mtime": str(item.get("mtime", "Aug  3  1985")),
            "size": 0,
            "nlink": 1,
        }
    _ensure_dir(nodes, "/lib")
    for item in catalog.get("lib") or []:
        name = str(item["name"])
        nodes[f"/lib/{name}"] = {
            "type": "file",
            "dir": False,
            "binary": True,
            "mode": "644",
            "uid": int(item.get("uid", 2)),
            "gid": int(item.get("gid", 2)),
            "size": int(item.get("size", 512)),
            "mtime": str(item.get("mtime", "Sep 25  1985")),
            "nlink": 1,
        }
    _apply_manifest(nodes, (manifest or {}).get("bec") or {}, root / "tree")
    _finish_links(nodes)
    return nodes


_GOLD_GEN = ""


def _gold_table(host: str) -> dict[str, dict]:
    global _GOLD_GEN
    from crossbar import site

    gen = site.gold_generation()
    if gen != _GOLD_GEN:
        _GOLD.clear()
        _GOLD_GEN = gen
    root = _pack_dir(host)
    kind = "mf" if host == "bec-mf" else "main"
    key = f"{root}:{kind}"
    if key not in _GOLD:
        _GOLD[key] = _build_gold(host)
    return _GOLD[key]


def _passwd_file(host: str) -> Path:
    root = _pack_dir(host)
    alt = root / "passwd"
    if alt.is_file():
        return alt
    return root / "tree" / "etc" / "passwd"


def _users(sess: Session) -> dict[str, dict]:
    found: dict[str, dict] = {}
    path = _passwd_file(sess.host)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, pw, uid, gid, gecos, home, shell = line.split(":")
            actual = pw
            if name == "root":
                actual = None
            elif pw == "x" and name in {"bin", "sys"}:
                actual = name
            elif pw == "x":
                actual = "\0locked"
            found[name] = {
                "name": name,
                "pw": actual,
                "uid": int(uid),
                "gid": int(gid),
                "gecos": gecos,
                "home": home,
                "shell": shell,
            }
    extra = _load_state(sess).get("users") or {}
    for name, row in extra.items():
        found[name] = row
    return found


def _passwd_text(sess: Session) -> str:
    lines = []
    for row in sorted(_users(sess).values(), key=lambda item: int(item["uid"])):
        pw = row["pw"]
        shown = "" if pw == "" else "x"
        lines.append(
            f"{row['name']}:{shown}:{row['uid']}:{row['gid']}:{row['gecos']}:{row['home']}:{row['shell']}"
        )
    return "\n".join(lines) + "\n"


def _root_password(host: str) -> str:
    path = _pack_dir(host) / "tree" / "usr" / "games" / "fortune"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("root password:"):
            return line.split(":", 1)[1].strip()
    return ""


def _owner_key(sess: Session) -> str:
    handle = sess.user or ""
    if handle and handle != "guest" and get_account(handle) is not None:
        return f"acct:{handle}"
    return f"sid:{sess.sid}"


def _stamp(sess: Session, code: str) -> bool:
    fresh = grant_achievement(_owner_key(sess), code)
    if code == "BEC-SYS":
        sess.ansi_ok = True
    return fresh


def _norm(cwd: str, raw: str) -> str:
    path = raw if raw.startswith("/") else (cwd.rstrip("/") + "/" + raw)
    parts: list[str] = []
    for piece in path.split("/"):
        if piece in {"", "."}:
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/" + "/".join(parts) if parts else "/"


def _meta(sess: Session, path: str) -> dict | None:
    gold = _gold_table(sess.host).get(path)
    state = _load_state(sess)
    if path in set(state.get("deleted") or []):
        return None
    overlay = (state.get("meta") or {}).get(path)
    if overlay and gold:
        merged = dict(gold)
        merged.update(overlay)
        return merged
    if overlay:
        return overlay
    return gold


def _is_dir(sess: Session, path: str) -> bool:
    info = _meta(sess, path)
    return bool(info and (info.get("dir") or info.get("type") == "dir"))


def _mode(info: dict) -> int:
    return int(str(info.get("mode", "644")), 8)


def _can(sess: Session, path: str, write: bool) -> bool:
    if sess.v7_uid == 0:
        if write and not _writable_place(sess, path):
            return path in {"/tmp"} or path.startswith("/tmp/") or _under_home(sess, path) or not write
        return True
    info = _meta(sess, path)
    if info is None and write:
        parent = path.rsplit("/", 1)[0] or "/"
        return _can(sess, parent, True)
    if info is None:
        return False
    mode = _mode(info)
    uid = int(info.get("uid", 0))
    gid = int(info.get("gid", 0))
    if sess.v7_uid == uid:
        bit = 0o200 if write else 0o400
    elif sess.v7_gid == gid:
        bit = 0o020 if write else 0o040
    else:
        bit = 0o002 if write else 0o004
    if mode & bit == 0:
        return False
    if write and not _writable_place(sess, path):
        return False
    return True


def _under_home(sess: Session, path: str) -> bool:
    home = _users(sess).get(sess.v7_user, {}).get("home", "/")
    return path == home or path.startswith(home.rstrip("/") + "/")


def _writable_place(sess: Session, path: str) -> bool:
    if sess.v7_uid == 0 and (path.startswith("/tmp/") or path.startswith("/usr/")):
        return True
    if path == "/tmp" or path.startswith("/tmp/"):
        return True
    return _under_home(sess, path)


def _read_bytes(sess: Session, path: str) -> str | None:
    if path == "/etc/passwd" and sess.host != "bec-mf":
        return _passwd_text(sess)
    state = _load_state(sess)
    if path in (state.get("files") or {}):
        return state["files"][path]
    info = _meta(sess, path)
    if not info or info.get("dir") or info.get("type") in {"exec", "dir"} or info.get("binary"):
        return None
    if info.get("type") == "dev":
        return "" if path == "/dev/null" else None
    rel = info.get("file")
    if not rel:
        return ""
    base = _pack_dir(sess.host) / ("mf-tree" if sess.host == "bec-mf" else "tree")
    file_path = base / rel
    if not file_path.is_file():
        return None
    return file_path.read_text(encoding="utf-8")


def _child_name(directory: str, key: str) -> str | None:
    if key == directory:
        return None
    if directory == "/":
        rest = key[1:] if key.startswith("/") else ""
    elif key.startswith(directory.rstrip("/") + "/"):
        rest = key[len(directory.rstrip("/")) + 1 :]
    else:
        return None
    if not rest:
        return None
    return rest.split("/", 1)[0]


def _children(sess: Session, path: str) -> list[str]:
    names: set[str] = set()
    state = _load_state(sess)
    deleted = set(state.get("deleted") or [])
    keys = list(_gold_table(sess.host))
    keys.extend(state.get("files") or {})
    keys.extend(state.get("meta") or {})
    for key in keys:
        if key in deleted:
            continue
        name = _child_name(path, key)
        if name:
            names.add(name)
    return sorted(names)


def _kind_char(info: dict) -> str:
    if info.get("type") == "dir" or info.get("dir"):
        return "d"
    if info.get("type") == "dev":
        return str(info.get("kind") or "c")
    return "-"


def _mode_string(mode: int, kind: str) -> str:
    bits = "rwxrwxrwx"
    text = [kind]
    for index in range(9):
        text.append(bits[index] if mode & (1 << (8 - index)) else "-")
    if mode & 0o1000 and kind == "d":
        text[-1] = "t" if text[-1] == "x" else "T"
    return "".join(text)


def _owner_name(sess: Session, uid: int) -> str:
    for name, row in _users(sess).items():
        if int(row["uid"]) == uid:
            return name
    return str(uid)


def _group_maps(host: str) -> tuple[dict[str, int], dict[int, str]]:
    path = _pack_dir(host) / "tree" / "etc" / "group"
    by_name: dict[str, int] = {}
    by_id: dict[int, str] = {}
    if not path.is_file():
        return by_name, by_id
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(":")
        if len(parts) < 3 or not str(parts[2]).isdigit():
            continue
        by_name[parts[0]] = int(parts[2])
        by_id[int(parts[2])] = parts[0]
    return by_name, by_id


def _group_name(sess: Session, gid: int) -> str:
    _by_name, by_id = _group_maps(sess.host)
    return by_id.get(gid, str(gid))


def _list_long(sess: Session, path: str, name: str | None = None) -> str:
    info = _meta(sess, path)
    if info is None:
        return ""
    shown = name if name is not None else (path.rsplit("/", 1)[-1] or "/")
    kind = _kind_char(info)
    nlink = int(info.get("nlink") or (2 if kind == "d" else 1))
    owner = _owner_name(sess, int(info.get("uid", 0)))
    group = _group_name(sess, int(info.get("gid", 0)))
    if info.get("type") == "dev":
        size_field = f"{int(info.get('major', 0))}, {int(info.get('minor', 0))}"
    elif info.get("type") in {"exec", "dir"} or info.get("binary") or kind == "d":
        size_field = str(int(info.get("size") or (512 if kind == "d" else 0)))
    else:
        body = _read_bytes(sess, path)
        size_field = str(len(body) if body is not None else int(info.get("size") or 0))
    return (
        f"{_mode_string(_mode(info), kind)} {nlink:2d} {owner:<8} {group:<8} "
        f"{size_field:>6} {_when(path, info)} {shown}"
    )


def _columns(names: list[str], width: int = 80) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    gap = 2
    widest = max(len(name) for name in names)
    cols = max(1, (width + gap) // (widest + gap))
    rows = (len(names) + cols - 1) // cols
    lines = []
    for row in range(rows):
        parts = []
        for col in range(cols):
            index = col * rows + row
            if index < len(names):
                parts.append(names[index].ljust(widest))
        lines.append((" " * gap).join(parts).rstrip())
    return "\r\n".join(lines)


def _when(path: str, info: dict) -> str:
    stamped = info.get("mtime")
    if stamped:
        return str(stamped)
    if path.startswith("/tmp"):
        return "Nov  8  1993"
    if path.startswith("/usr/hank") or path.startswith("/usr/spool/mail"):
        return "Nov 12  1993"
    if "budget.91" in path or path.startswith("/usr/adm"):
        return "Nov  2  1991"
    if path.startswith("/usr/pat"):
        return "Apr 19  1990"
    if path.startswith("/bin") or path.startswith("/README"):
        return "Sep 25  1985"
    return "Jun 18  1987"




def enter(sess: Session) -> str:
    push_hop(sess, "bec", 1200)
    sess.v7_phase = "login"
    sess.v7_user = ""
    sess.v7_uid = -1
    sess.v7_cwd = "/"
    sess.v7_pending = ""
    sess.v7_epoch = time.monotonic()
    sess.lead_sleep = 1.2
    return (
        "CONNECT 1200\r\n"
        "\r\n"
        "BIG-EVIL CORPORATION\r\n"
        "UNIX V7  (PDP-11/70)   last sysadmin login: 12-Nov-93\r\n"
        "dialup 1200  tty02\r\n"
        f"{_now().strftime('%a %b %d %H:%M:%S %Z %Y')}\r\n"
        "\r\n"
    )


def hangup(sess: Session) -> str:
    pop_hop(sess)
    if sess.host == "bec":
        if sess.v7_user:
            sess.v7_phase = "shell"
        return "disconnected\r\n"
    if _registered(sess) is None:
        sess.v7_store = {}
    sess.v7_phase = ""
    sess.v7_user = ""
    sess.v7_uid = -1
    sess.v7_gid = -1
    sess.v7_pending = ""
    sess.v7_path = "/bin:/usr/bin"
    return "NO CARRIER\r\n"


def _login_as(sess: Session, row: dict) -> str:
    sess.v7_phase = "shell"
    sess.v7_user = row["name"]
    sess.v7_uid = int(row["uid"])
    sess.v7_gid = int(row["gid"])
    sess.v7_cwd = row["home"] if row["home"] != "/" else "/"
    sess.v7_pending = ""
    if row["name"] == "guest":
        _stamp(sess, "BEC-GUEST")
    if sess.v7_uid == 3:
        _stamp(sess, "BEC-SYS")
    if sess.v7_uid == 0:
        _stamp(sess, "BEC-ROOT")
    _apply_path(sess)
    motd = (_read_bytes(sess, "/etc/motd") or "").replace("\n", "\r\n")
    speed = sess.baud_now
    return f"[{speed}]\r\n{motd}\r\n"


def _apply_path(sess: Session) -> None:
    sess.v7_path = str(_catalog(sess.host).get("path") or "/bin:/usr/bin")
    home = _users(sess).get(sess.v7_user, {}).get("home", "/")
    if not home or home == "/":
        return
    text = _read_bytes(sess, home.rstrip("/") + "/.profile") or ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("PATH="):
            sess.v7_path = stripped.split("=", 1)[1].strip()


def _check_pw(sess: Session, row: dict, secret: str) -> bool:
    if row["pw"] is None:
        return secret == _root_password(sess.host)
    return secret == row["pw"]


def _on_path(sess: Session, name: str) -> dict | None:
    for directory in (sess.v7_path or "/bin:/usr/bin").split(":"):
        if not directory:
            continue
        info = _meta(sess, directory.rstrip("/") + "/" + name)
        if info and info.get("type") == "exec":
            return info
    return None


# Shell builtins win over a catalog file of the same name (/bin/cd still runs cd).
_BUILTINS = frozenset({"cd", "pwd", "exit", "logout", "bye", "export", "wait"})


def interrupt(sess: Session) -> str:
    """^C. Leave ed, pico, a pending sleep, or a cu hop. Never answers with ?."""
    sess.lead_sleep = 0.0
    if sess.v7_phase in {"pico", "ed"}:
        sess.v7_phase = "shell"
        sess.ed_dirty = False
        sess.pico_ask = ""
    if sess.v7_phase == "password" and str(sess.v7_pending).startswith("su:"):
        sess.v7_phase = "shell"
        sess.v7_pending = ""
    if sess.host == "bec-mf":
        return hangup(sess)
    return ""


def _expand(sess: Session, token: str) -> str:
    if token == "$?":
        return str(sess.v7_status)
    return token


def _note_status(sess: Session, text: str) -> None:
    if sess.v7_status != 0 or not text:
        return
    first = text.split("\r\n", 1)[0]
    marks = (
        ": usage:",
        ": not found",
        ": can't",
        ": cannot",
        ": illegal",
        "permission denied",
        ": sorry",
        ": no such",
    )
    if any(mark in first for mark in marks):
        sess.v7_status = 1


def _miss(sess: Session, name: str) -> str:
    sess.v7_status = 127
    return f"{name}: not found\r\n"


def _argv0(cmd: str) -> str:
    return cmd.rsplit("/", 1)[-1] if "/" in cmd else cmd


def _run_impl(sess: Session, name: str, args: list[str]) -> str:
    if name in _REFUSED or name not in _COMMANDS:
        return _miss(sess, name)
    text = _COMMANDS[name](sess, args)
    _note_status(sess, text)
    return text


def on_line(sess: Session, text: str) -> str:
    if sess.v7_phase == "ed":
        return _ed_line(sess, text)
    line = text.strip()
    if sess.v7_phase in {"", "login"}:
        return _at_login(sess, line)
    if sess.v7_phase == "password":
        return _at_password(sess, text)
    if line in {"~.", "~"} and sess.host == "bec-mf":
        return hangup(sess)
    if not line:
        return ""
    try:
        parts = shlex.split(line)
    except ValueError:
        sess.v7_status = 1
        return "syntax error\r\n"
    cmd = parts[0]
    args = [_expand(sess, arg) for arg in parts[1:]]
    sess.v7_status = 0
    base = _argv0(cmd)
    if base in _BUILTINS:
        return _run_impl(sess, base, args)
    if "/" in cmd:
        path = _norm(sess.v7_cwd, cmd)
        info = _meta(sess, path)
        if info is None:
            return _miss(sess, cmd)
        if info.get("type") != "exec":
            sess.v7_status = 126
            return f"{cmd}: cannot execute\r\n"
        return _run_impl(sess, base, args)
    if cmd in _COMMANDS and cmd not in _REFUSED:
        return _run_impl(sess, cmd, args)
    if _on_path(sess, cmd):
        return _miss(sess, cmd)
    return _miss(sess, cmd)


def _at_login(sess: Session, line: str) -> str:
    if line in {"logout", "exit", "bye"}:
        return hangup(sess)
    if line == "":
        return ""
    row = _users(sess).get(line)
    if row is None:
        return "Login incorrect\r\n"
    if row["pw"] == "":
        return _login_as(sess, row)
    sess.v7_phase = "password"
    sess.v7_pending = line
    return ""


def _at_password(sess: Session, text: str) -> str:
    if text.strip() in {"logout", "exit", "bye"}:
        return hangup(sess)
    pending = sess.v7_pending
    if pending.startswith("su:"):
        target = pending.split(":", 1)[1]
        row = _users(sess).get(target)
        if row and _check_pw(sess, row, text):
            return _login_as(sess, row)
        sess.v7_phase = "shell"
        sess.v7_pending = ""
        sess.v7_status = 1
        return "su: sorry\r\n"
    row = _users(sess).get(pending)
    if row and _check_pw(sess, row, text):
        return _login_as(sess, row)
    sess.v7_phase = "login"
    sess.v7_pending = ""
    return "Login incorrect\r\n"


def _cmd_whoami(sess: Session, args: list[str]) -> str:
    return f"{sess.v7_user}\r\n"


def _cmd_id(sess: Session, args: list[str]) -> str:
    group = _group_name(sess, sess.v7_gid)
    return f"uid={sess.v7_uid}({sess.v7_user}) gid={sess.v7_gid}({group})\r\n"


def _cmd_pwd(sess: Session, args: list[str]) -> str:
    return f"{sess.v7_cwd}\r\n"


def _cmd_cd(sess: Session, args: list[str]) -> str:
    home = _users(sess).get(sess.v7_user, {}).get("home", "/") or "/"
    if not args or args[0] in {"~", ""}:
        raw = home
        shown = home
    elif args[0].startswith("~/"):
        raw = home.rstrip("/") + "/" + args[0][2:]
        shown = args[0]
    else:
        raw = args[0]
        shown = args[0]
    path = _norm(sess.v7_cwd, raw)
    if not _is_dir(sess, path) or not _can(sess, path, False):
        sess.v7_status = 1
        return f"cd: can't change to {shown}\r\n"
    sess.v7_cwd = path
    return ""


def _cmd_ls(sess: Session, args: list[str]) -> str:
    long = False
    show_all = False
    paths = []
    for arg in args:
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "l":
                    long = True
                elif flag == "a":
                    show_all = True
                else:
                    return f"ls: illegal option -{flag}\r\n"
        else:
            paths.append(arg)
    if not paths:
        paths = ["."]
    blocks = []
    for raw in paths:
        path = _norm(sess.v7_cwd, raw)
        if _meta(sess, path) is None and path != "/":
            return f"ls: {raw}: not found\r\n"
        if not _can(sess, path, False):
            return "ls: permission denied\r\n"
        if _is_dir(sess, path):
            names = _children(sess, path)
            if not show_all:
                names = [name for name in names if not name.startswith(".")]
            lines: list[str] = []
            if show_all:
                parent = path.rsplit("/", 1)[0] or "/"
                lines.append(_list_long(sess, path, ".") if long else ".")
                lines.append(_list_long(sess, parent, "..") if long else "..")
            if long:
                lines.extend(_list_long(sess, _norm(path, name)) for name in names)
                blocks.append("\r\n".join(line for line in lines if line))
            elif show_all:
                blocks.append(_columns([".", "..", *names]))
            else:
                blocks.append(_columns(names))
        elif long:
            blocks.append(_list_long(sess, path))
        else:
            blocks.append(path.rsplit("/", 1)[-1])
    return "\r\n".join(blocks) + ("\r\n" if blocks else "")


def _show_file(sess: Session, raw: str) -> str:
    path = _norm(sess.v7_cwd, raw)
    info = _meta(sess, path)
    if _is_dir(sess, path):
        return f"cat: {raw}: directory\r\n"
    if info is None:
        return f"cat: {raw}: not found\r\n"
    if not _can(sess, path, False):
        return f"cat: cannot open {raw}\r\n"
    if info.get("type") == "exec" or info.get("binary"):
        return f"cat: {path}: cannot execute as text\r\n"
    if info.get("type") == "dev":
        if path == "/dev/null":
            return ""
        return f"cat: {path}: cannot open\r\n"
    body = _read_bytes(sess, path) or ""
    if sess.host == "bec-mf" and path == "/usr/payroll/memo":
        _stamp(sess, "BEC-MF")
    return body.replace("\n", "\r\n") if body.endswith("\n") else body.replace("\n", "\r\n") + "\r\n"


def _cmd_cat(sess: Session, args: list[str]) -> str:
    if not args:
        return "cat: missing file\r\n"
    return "".join(_show_file(sess, arg) for arg in args)


def _cmd_more(sess: Session, args: list[str]) -> str:
    return _cmd_cat(sess, args)


def _cmd_head(sess: Session, args: list[str]) -> str:
    count = 10
    files = []
    index = 0
    while index < len(args):
        if args[index] == "-n" and index + 1 < len(args):
            count = int(args[index + 1])
            index += 2
            continue
        files.append(args[index])
        index += 1
    if not files:
        return "head: missing file\r\n"
    text = _show_file(sess, files[0])
    if text.startswith("cat:"):
        return text
    return "".join(text.split("\r\n")[:count]) + "\r\n" if text else ""


def _cmd_tail(sess: Session, args: list[str]) -> str:
    count = 10
    files = []
    index = 0
    while index < len(args):
        if args[index] == "-n" and index + 1 < len(args):
            count = int(args[index + 1])
            index += 2
            continue
        files.append(args[index])
        index += 1
    if not files:
        return "tail: missing file\r\n"
    text = _show_file(sess, files[0])
    if text.startswith("cat:"):
        return text
    lines = [line for line in text.split("\r\n") if line != ""]
    return "\r\n".join(lines[-count:]) + "\r\n"


def _cmd_grep(sess: Session, args: list[str]) -> str:
    if len(args) < 2:
        return "grep: usage: grep pattern file\r\n"
    pattern, raw = args[0], args[1]
    text = _show_file(sess, raw)
    if text.startswith("cat:"):
        return text
    hits = [line for line in text.split("\r\n") if pattern in line]
    return ("\r\n".join(hits) + "\r\n") if hits else ""


def _cmd_echo(sess: Session, args: list[str]) -> str:
    return " ".join(args) + "\r\n"


def _cmd_date(sess: Session, args: list[str]) -> str:
    return _now().strftime("%a %b %d %H:%M:%S %Z %Y") + "\r\n"


def _cmd_who(sess: Session, args: list[str]) -> str:
    stamp = _now().strftime("%b %d %H:%M")
    return f"{sess.v7_user:<10}tty02        {stamp}\r\n"


def _cmd_w(sess: Session, args: list[str]) -> str:
    stamp = _now().strftime("%H:%M")
    return f" {stamp}  1 user,  nobody else since Nov 12\r\n{_cmd_who(sess, args)}"


def _cmd_last(sess: Session, args: list[str]) -> str:
    now = _now().strftime("%a %b %d %H:%M")
    return (
        f"{sess.v7_user:<8} tty02    {now}   still logged in\r\n"
        "hank     ttyh0    Fri Nov 12 18:02 - 18:40  (1993)\r\n"
        "pat      console  Mon Oct  4 09:15 - 17:02  (1993)\r\n"
        "hank     tty02    Tue Mar  3 22:11 - 22:40  (1992)\r\n"
        "sys      console  Wed Jun 18 11:00 - 11:05  (1991)\r\n"
        "\r\nwtmp begins Sep 25 1985\r\n"
    )


def _cmd_ps(sess: Session, args: list[str]) -> str:
    return (
        " PID TTY TIME CMD\r\n"
        "   1 -   0:12 init\r\n"
        "   8 -   0:00 update\r\n"
        "   9 -   0:00 cron\r\n"
        "  12 02  0:00 getty\r\n"
        f"  44 02  0:00 -sh {sess.v7_user}\r\n"
    )


def _cmd_su(sess: Session, args: list[str]) -> str:
    target = args[0] if args and args[0] else "root"
    row = _users(sess).get(target)
    if row is None:
        sess.v7_status = 1
        return "su: unknown id\r\n"
    if sess.v7_uid == 0:
        return _login_as(sess, row)
    if row["pw"] == "":
        return _login_as(sess, row)
    sess.v7_phase = "password"
    sess.v7_pending = f"su:{target}"
    return ""


def _cmd_passwd(sess: Session, args: list[str]) -> str:
    users = _users(sess)
    if len(args) == 1 and sess.v7_uid == 0:
        name, secret = args[0], ""
    elif len(args) == 2 and sess.v7_uid == 0:
        name, secret = args
    elif len(args) == 1:
        name, secret = sess.v7_user, args[0]
    else:
        return "passwd: usage: passwd [user] secret\r\n"
    if name != sess.v7_user and sess.v7_uid != 0:
        return "passwd: permission denied\r\n"
    if name not in users:
        return "passwd: unknown id\r\n"
    state = _load_state(sess)
    row = dict(users[name])
    row["pw"] = secret
    state.setdefault("users", {})[name] = row
    _save_state(sess, state)
    return "passwd: password changed\r\n"


def _cmd_useradd(sess: Session, args: list[str]) -> str:
    if sess.v7_uid != 0:
        return "useradd: permission denied\r\n"
    if len(args) != 1 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,11}", args[0]):
        return "useradd: usage: useradd name\r\n"
    name = args[0].lower()
    if name in _users(sess):
        return "useradd: already exists\r\n"
    state = _load_state(sess)
    taken = [int(row["uid"]) for row in _users(sess).values()]
    uid = max([99, *taken]) + 1
    row = {
        "name": name,
        "pw": "",
        "uid": uid,
        "gid": 9,
        "gecos": "added on bec",
        "home": f"/usr/{name}",
        "shell": "/bin/sh",
    }
    state.setdefault("users", {})[name] = row
    home = row["home"]
    state.setdefault("meta", {})[home] = {
        "type": "dir",
        "dir": True,
        "mode": "755",
        "uid": uid,
        "gid": 9,
        "mtime": "Nov 12  1993",
        "size": 512,
        "nlink": 2,
    }
    _save_state(sess, state)
    if sess.user and name == sess.user.lower():
        _stamp(sess, "BEC-USERADD")
    return f"useradd: {name} uid {uid}\r\n"


def _cmd_chmod(sess: Session, args: list[str]) -> str:
    if len(args) != 2:
        return "chmod: usage: chmod mode file\r\n"
    path = _norm(sess.v7_cwd, args[1])
    info = _meta(sess, path)
    if info is None:
        return "chmod: not found\r\n"
    if sess.v7_uid not in {0, int(info.get("uid", 0))}:
        return "chmod: permission denied\r\n"
    try:
        mode = int(args[0], 8)
    except ValueError:
        return "chmod: bad mode\r\n"
    state = _load_state(sess)
    meta = dict(info)
    meta["mode"] = format(mode, "o")
    state.setdefault("meta", {})[path] = meta
    _save_state(sess, state)
    return ""


def _cmd_chown(sess: Session, args: list[str]) -> str:
    if sess.v7_uid != 0:
        return "chown: permission denied\r\n"
    if len(args) != 2:
        return "chown: usage: chown user file\r\n"
    spec = args[0]
    group_name = None
    user_name = spec
    if "." in spec:
        user_name, group_name = spec.split(".", 1)
    path = _norm(sess.v7_cwd, args[1])
    info = _meta(sess, path)
    owner = _users(sess).get(user_name)
    if info is None or owner is None:
        return "chown: not found\r\n"
    gid = int(owner["gid"])
    if group_name is not None:
        by_name, _by_id = _group_maps(sess.host)
        if group_name not in by_name:
            return "chown: not found\r\n"
        gid = by_name[group_name]
    state = _load_state(sess)
    meta = dict(info)
    meta["uid"] = int(owner["uid"])
    meta["gid"] = gid
    state.setdefault("meta", {})[path] = meta
    _save_state(sess, state)
    return ""


def _cmd_touch(sess: Session, args: list[str]) -> str:
    if len(args) != 1:
        return "touch: usage: touch file\r\n"
    path = _norm(sess.v7_cwd, args[0])
    if not _writable_place(sess, path):
        return "touch: permission denied\r\n"
    state = _load_state(sess)
    state.setdefault("files", {}).setdefault(path, "")
    state.setdefault("meta", {})[path] = {
        "type": "file",
        "dir": False,
        "mode": "644",
        "uid": sess.v7_uid,
        "gid": sess.v7_gid,
        "mtime": "Nov  8  1993",
        "nlink": 1,
    }
    deleted = set(state.get("deleted") or [])
    deleted.discard(path)
    state["deleted"] = sorted(deleted)
    _save_state(sess, state)
    return ""


def _cmd_rm(sess: Session, args: list[str]) -> str:
    if len(args) != 1:
        return "rm: usage: rm file\r\n"
    path = _norm(sess.v7_cwd, args[0])
    if not _writable_place(sess, path) or _is_dir(sess, path):
        return "rm: permission denied\r\n"
    info = _meta(sess, path)
    parent = path.rsplit("/", 1)[0] or "/"
    parent_info = _meta(sess, parent)
    sticky = bool(parent_info) and _mode(parent_info) & 0o1000
    if sticky and info and sess.v7_uid not in {0, int(info.get("uid", -1))}:
        return "rm: permission denied\r\n"
    state = _load_state(sess)
    state.setdefault("deleted", []).append(path)
    (state.get("files") or {}).pop(path, None)
    _save_state(sess, state)
    return ""


def _cmd_cp(sess: Session, args: list[str]) -> str:
    if len(args) != 2:
        return "cp: usage: cp src dest\r\n"
    src = _norm(sess.v7_cwd, args[0])
    dest = _norm(sess.v7_cwd, args[1])
    body = _show_file(sess, args[0])
    if body.startswith("cat:"):
        return body
    if not _writable_place(sess, dest):
        return "cp: permission denied\r\n"
    state = _load_state(sess)
    state.setdefault("files", {})[dest] = body.replace("\r\n", "\n")
    state.setdefault("meta", {})[dest] = {
        "type": "file",
        "dir": False,
        "mode": "644",
        "uid": sess.v7_uid,
        "gid": sess.v7_gid,
        "mtime": "Nov  8  1993",
        "nlink": 1,
    }
    _save_state(sess, state)
    return ""


def _cmd_mv(sess: Session, args: list[str]) -> str:
    copied = _cmd_cp(sess, args)
    if copied:
        return copied
    return _cmd_rm(sess, args[:1])


def _cmd_mail(sess: Session, args: list[str]) -> str:
    user = args[0] if args else sess.v7_user
    for path in (f"/usr/spool/mail/{user}", f"/usr/mail/{user}"):
        info = _meta(sess, path)
        if info and not (info.get("dir") or info.get("type") == "dir"):
            if not _can(sess, path, False):
                return "mail: permission denied\r\n"
            return _show_file(sess, path)
    return "No mail.\r\n"


def _cmd_man(sess: Session, args: list[str]) -> str:
    if not args:
        return "man: what?\r\n"
    page = f"/usr/man/man1/{args[0]}.1"
    if _meta(sess, page) is None:
        return f"man: {args[0]}: not found\r\nTry man ls\r\n"
    return _show_file(sess, page)


def _cmd_uname(sess: Session, args: list[str]) -> str:
    catalog = _catalog(sess.host)
    if sess.host == "bec-mf":
        full = str(catalog.get("uname_mf") or catalog.get("uname") or "unix")
        short = "BEC-MF"
    else:
        full = str(catalog.get("uname") or "unix")
        short = "BEC"
    if not args:
        return full + "\r\n"
    pieces: list[str] = []
    for arg in args:
        if not arg.startswith("-") or arg == "-":
            sess.v7_status = 1
            return "uname: usage: uname [-sna]\r\n"
        for flag in arg[1:]:
            if flag in {"s", "n"}:
                pieces.append(short)
            elif flag == "a":
                pieces.append(full)
            else:
                sess.v7_status = 1
                return "uname: usage: uname [-sna]\r\n"
    return " ".join(pieces) + "\r\n"


def _cmd_hostname(sess: Session, args: list[str]) -> str:
    return ("bec-mf\r\n" if sess.host == "bec-mf" else "bec\r\n")


def _cmd_stty(sess: Session, args: list[str]) -> str:
    clock = _now().strftime("%a %b %d %H:%M:%S %Z %Y")
    return f"speed {sess.baud_now} baud; line = tty02; {clock}\r\n"


def _cmd_cu(sess: Session, args: list[str]) -> str:
    if sess.v7_uid != 0:
        return "cu: permission denied\r\n"
    target = args[-1] if args else ""
    if target in {"/dev/ttyh0", "ttyh0"}:
        target = "bec-mf"
    if target not in {"bec-mf", "mainframe"}:
        return "cu: usage: cu bec-mf\r\n"
    if sess.host == "bec-mf":
        return "cu: already there\r\n"
    push_hop(sess, "bec-mf", 9600)
    sess.v7_cwd = "/"
    return (
        "CONNECT 1200\r\n"
        "bec-mf ceiling 9600; path stays 1200\r\n"
        "\r\n"
        "BIG-EVIL MAINFRAME\r\n"
        "logged in as root\r\n"
        "\r\n"
    )


def _cmd_claim(sess: Session, args: list[str]) -> str:
    if not args:
        return "claim: usage: claim id\r\n"
    code = args[0].upper()
    known = {"BEC-GUEST", "BEC-SYS", "BEC-ROOT", "BEC-USERADD", "BEC-MF"}
    if code not in known:
        # Event flags are chain claims. Local stamps stay the names above.
        from crossbar.commands import perform_claim

        return perform_claim(sess, args[0])
    allowed = (
        (code == "BEC-GUEST" and sess.v7_user == "guest")
        or (code == "BEC-SYS" and sess.v7_uid == 3)
        or (code == "BEC-ROOT" and sess.v7_uid == 0)
        or (code == "BEC-MF" and code in achievements_for(_owner_key(sess)))
        or (code == "BEC-USERADD" and code in achievements_for(_owner_key(sess)))
    )
    if code in {"BEC-ROOT", "BEC-SYS", "BEC-GUEST"} and allowed:
        fresh = _stamp(sess, code)
        return "claim: already stamped\r\n" if not fresh else f"claim: {code}\r\n"
    if code in achievements_for(_owner_key(sess)):
        return "claim: already stamped\r\n"
    return "claim: permission denied\r\n"


def _cmd_logout(sess: Session, args: list[str]) -> str:
    return hangup(sess)


def write_virtual(sess: Session, path: str, content: str) -> str:
    if not path:
        return "No file name"
    if not _can(sess, path, True):
        return "permission denied"
    state = _load_state(sess)
    previous = _meta(sess, path) or {}
    state.setdefault("files", {})[path] = content
    state.setdefault("meta", {})[path] = {
        "type": "file",
        "dir": False,
        "mode": str(previous.get("mode", "644")),
        "uid": int(previous.get("uid", sess.v7_uid)),
        "gid": int(previous.get("gid", sess.v7_gid)),
        "mtime": str(previous.get("mtime") or "Nov  8  1993"),
        "nlink": 1,
    }
    deleted = set(state.get("deleted") or [])
    deleted.discard(path)
    state["deleted"] = sorted(deleted)
    _save_state(sess, state)
    return ""


def _cmd_help(sess: Session, args: list[str]) -> str:
    return "help: not found\r\nTry man ls\r\n"


def _cmd_pico(sess: Session, args: list[str]) -> str:
    from crossbar.pico import full_paint

    if not args:
        sess.pico_path = ""
        sess.pico_lines = [""]
        sess.pico_ro = False
    else:
        path = _norm(sess.v7_cwd, args[0])
        info = _meta(sess, path)
        if info and (info.get("dir") or info.get("type") == "dir"):
            return "pico: directory\r\n"
        if info and (info.get("type") in {"exec", "dev"} or info.get("binary")):
            return "pico: not a text file\r\n"
        if info is None:
            if not _can(sess, path, True):
                return "pico: permission denied\r\n"
            text = ""
            readonly = False
        else:
            if not _can(sess, path, False):
                return "pico: permission denied\r\n"
            text = _read_bytes(sess, path) or ""
            readonly = not _can(sess, path, True)
        sess.pico_path = path
        sess.pico_lines = text.split("\n")
        if sess.pico_lines and sess.pico_lines[-1] == "":
            sess.pico_lines.pop()
        if not sess.pico_lines:
            sess.pico_lines = [""]
        sess.pico_ro = readonly
    sess.pico_row = 0
    sess.pico_col = 0
    sess.pico_top = 0
    sess.pico_dirty = False
    sess.pico_ask = ""
    sess.pico_search = ""
    sess.v7_phase = "pico"
    return full_paint(sess)


def _cmd_ed(sess: Session, args: list[str]) -> str:
    path = _norm(sess.v7_cwd, args[0]) if args else ""
    info = _meta(sess, path) if path else None
    if info and (info.get("type") in {"exec", "dev"} or info.get("binary")):
        return "ed: not a text file\r\n"
    if path and info and not _can(sess, path, False):
        return "ed: permission denied\r\n"
    text = _read_bytes(sess, path) if path and _meta(sess, path) else ""
    sess.ed_lines = (text or "").split("\n")
    if sess.ed_lines and sess.ed_lines[-1] == "":
        sess.ed_lines.pop()
    if not sess.ed_lines:
        sess.ed_lines = []
    sess.ed_path = path
    sess.ed_dirty = False
    sess.v7_phase = "ed"
    return f"{len(text or '')}\r\n"


def _ed_line(sess: Session, text: str) -> str:
    line = text.strip()
    if line == "q":
        if sess.ed_dirty:
            sess.ed_dirty = False
            return "?\r\n"
        sess.v7_phase = "shell"
        return ""
    if line == "Q":
        sess.v7_phase = "shell"
        sess.ed_dirty = False
        return ""
    if line == "w":
        if not sess.ed_path:
            return "?\r\n"
        err = write_virtual(sess, sess.ed_path, "\n".join(sess.ed_lines) + "\n")
        if err:
            return f"ed: {err}\r\n"
        sess.ed_dirty = False
        return f"{len(''.join(sess.ed_lines))}\r\n"
    if line == ",p":
        return "".join(f"{item}\r\n" for item in sess.ed_lines)
    if len(line) >= 2 and line.startswith("/") and line.endswith("/"):
        pattern = line[1:-1]
        for item in sess.ed_lines:
            if pattern in item:
                return f"{item}\r\n"
        return "?\r\n"
    return "?\r\n"


def _cmd_vi(sess: Session, args: list[str]) -> str:
    return "vi: broken: termcap\r\n"


def _cmd_wc(sess: Session, args: list[str]) -> str:
    if not args:
        return "wc: missing file\r\n"
    text = _show_file(sess, args[0])
    if text.startswith("cat:"):
        return text
    lines = text.split("\r\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    words = text.split()
    return f"{len(lines):7d}{len(words):8d}{len(text):8d} {args[0]}\r\n"


def _file_one(sess: Session, raw: str) -> str:
    path = _norm(sess.v7_cwd, raw)
    info = _meta(sess, path)
    if info is None:
        sess.v7_status = 1
        return f"file: {raw}: cannot open\r\n"
    if info.get("type") == "exec":
        return f"{path}: PDP-11 executable\r\n"
    if info.get("type") == "dir" or info.get("dir"):
        return f"{path}: directory\r\n"
    if info.get("type") == "dev":
        kind = "block" if info.get("kind") == "b" else "character"
        return f"{path}: {kind} special\r\n"
    name = path.rsplit("/", 1)[-1]
    if name == "core":
        return f"{path}: core dumped\r\n"
    if info.get("binary"):
        return f"{path}: data\r\n"
    body = _read_bytes(sess, path) or ""
    kind = "empty" if body.strip() == "" else "ascii text"
    return f"{path}: {kind}\r\n"


def _cmd_file(sess: Session, args: list[str]) -> str:
    if not args:
        sess.v7_status = 1
        return "file: usage: file file ...\r\n"
    return "".join(_file_one(sess, raw) for raw in args)


def _cmd_od(sess: Session, args: list[str]) -> str:
    if not args:
        return "od: missing file\r\n"
    text = _show_file(sess, args[0])
    if text.startswith("cat:"):
        return text
    raw = text.encode("utf-8", "replace")[:256]
    rows = []
    for index in range(0, len(raw), 16):
        chunk = raw[index : index + 16]
        hexes = " ".join(f"{byte:02x}" for byte in chunk)
        rows.append(f"{index:07o}  {hexes}")
    return ("\r\n".join(rows) + "\r\n") if rows else ""


def _cmd_cmp(sess: Session, args: list[str]) -> str:
    if len(args) != 2:
        return "cmp: usage: cmp file1 file2\r\n"
    left = _show_file(sess, args[0])
    right = _show_file(sess, args[1])
    if left.startswith("cat:") or right.startswith("cat:"):
        return left if left.startswith("cat:") else right
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return f"{args[0]} {args[1]} differ: byte {index + 1}\r\n"
    if len(left) != len(right):
        return f"{args[0]} {args[1]} differ: byte {min(len(left), len(right)) + 1}\r\n"
    return ""


def _cmd_diff(sess: Session, args: list[str]) -> str:
    if len(args) != 2:
        return "diff: usage: diff file1 file2\r\n"
    left = _show_file(sess, args[0])
    right = _show_file(sess, args[1])
    if left.startswith("cat:") or right.startswith("cat:"):
        return left if left.startswith("cat:") else right
    if left == right:
        return ""
    out = [f"< {line}" for line in left.split("\r\n") if line]
    out += [f"> {line}" for line in right.split("\r\n") if line]
    return "\r\n".join(out) + "\r\n"


def _cmd_sort(sess: Session, args: list[str]) -> str:
    if not args:
        return "sort: missing file\r\n"
    text = _show_file(sess, args[0])
    if text.startswith("cat:"):
        return text
    lines = [line for line in text.split("\r\n") if line != ""]
    return "\r\n".join(sorted(lines)) + ("\r\n" if lines else "")


def _cmd_uniq(sess: Session, args: list[str]) -> str:
    if not args:
        return "uniq: missing file\r\n"
    text = _show_file(sess, args[0])
    if text.startswith("cat:"):
        return text
    lines = [line for line in text.split("\r\n") if line != ""]
    kept: list[str] = []
    for line in lines:
        if not kept or kept[-1] != line:
            kept.append(line)
    return "\r\n".join(kept) + ("\r\n" if kept else "")


def _cmd_cut(sess: Session, args: list[str]) -> str:
    if len(args) < 3 or args[0] != "-d" or not args[2].startswith("-f"):
        return "cut: usage: cut -d delim -f N file\r\n"
    field = int(args[2][2:] or args[3])
    filename = args[-1]
    text = _show_file(sess, filename)
    if text.startswith("cat:"):
        return text
    rows = []
    for line in text.split("\r\n"):
        if line == "":
            continue
        parts = line.split(args[1])
        rows.append(parts[field - 1] if 0 < field <= len(parts) else "")
    return "\r\n".join(rows) + ("\r\n" if rows else "")


def _cmd_tr(sess: Session, args: list[str]) -> str:
    if len(args) < 2:
        return "tr: usage: tr from to\r\n"
    table = str.maketrans(args[0], args[1])
    return " ".join(args[2:]).translate(table) + "\r\n"


def _cmd_tee(sess: Session, args: list[str]) -> str:
    if len(args) < 2:
        return "tee: usage: tee file text...\r\n"
    path = _norm(sess.v7_cwd, args[0])
    body = " ".join(args[1:]) + "\n"
    if not (path.startswith("/tmp/") or path == "/tmp"):
        return "tee: permission denied\r\n"
    err = write_virtual(sess, path, body)
    if err:
        return f"tee: {err}\r\n"
    return body.replace("\n", "\r\n")


def _cmd_test(sess: Session, args: list[str]) -> str:
    if args and args[-1] == "]":
        args = args[:-1]
    if len(args) != 2 or args[0] not in {"-f", "-d", "-r"}:
        return "test: usage: test -f file\r\n"
    path = _norm(sess.v7_cwd, args[1])
    info = _meta(sess, path)
    if args[0] == "-d":
        ok = bool(info and info.get("dir"))
    elif args[0] == "-r":
        ok = bool(info) and _can(sess, path, False)
    else:
        ok = bool(info) and not info.get("dir")
    return "" if ok else "false\r\n"


def _cmd_expr(sess: Session, args: list[str]) -> str:
    if len(args) != 3 or args[1] not in {"+", "-", "*", "/"}:
        return "expr: usage: expr n op n\r\n"
    try:
        left, right = int(args[0]), int(args[2])
    except ValueError:
        return "expr: non-numeric\r\n"
    if args[1] == "+":
        value = left + right
    elif args[1] == "-":
        value = left - right
    elif args[1] == "*":
        value = left * right
    else:
        if right == 0:
            return "expr: division by zero\r\n"
        value = left // right
    return f"{value}\r\n"


def _cmd_sleep(sess: Session, args: list[str]) -> str:
    if not args:
        sess.v7_status = 1
        return "sleep: usage: sleep seconds\r\n"
    try:
        seconds = float(args[0])
    except ValueError:
        sess.v7_status = 1
        return "sleep: usage: sleep seconds\r\n"
    sess.lead_sleep += min(max(seconds, 0), 5)
    return ""


def _cmd_clear(sess: Session, args: list[str]) -> str:
    return "\x1b[2J\x1b[H"


def _cmd_tput(sess: Session, args: list[str]) -> str:
    if args == ["clear"]:
        return _cmd_clear(sess, args)
    return "tput: not implemented\r\n"


def _cmd_df(sess: Session, args: list[str]) -> str:
    rows = _catalog(sess.host).get("df") or []
    if not rows:
        return "df: no volumes\r\n"
    lines = ["Filesystem  mounted  blocks  used  free  %"]
    for row in rows:
        lines.append(
            f"{str(row.get('mount', '')):<12}{str(row.get('dev', '')):<9}"
            f"{int(row.get('blocks', 0)):<8}{int(row.get('used', 0)):<6}"
            f"{int(row.get('free', 0)):<6}{row.get('pct', '')}"
        )
    return "\r\n".join(lines) + "\r\n"


def _cmd_du(sess: Session, args: list[str]) -> str:
    raw = args[0] if args else "."
    path = _norm(sess.v7_cwd, raw)
    if _meta(sess, path) is None:
        return f"du: {raw}: not found\r\n"
    total = 0
    prefix = path.rstrip("/")
    for key, info in _gold_table(sess.host).items():
        if key == path or key.startswith(prefix + "/"):
            if info.get("type") == "dir" or info.get("dir"):
                total += int(info.get("size") or 512)
            else:
                total += int(info.get("size") or 0)
    blocks = max(1, (total + 511) // 512)
    return f"{blocks}\t{path}\r\n"


def _cmd_tty(sess: Session, args: list[str]) -> str:
    return "/dev/tty02\r\n"


def _cmd_mesg(sess: Session, args: list[str]) -> str:
    if not args:
        return f"is {sess.v7_mesg}\r\n"
    if args[0] not in {"y", "n"}:
        return "mesg: usage: mesg y|n\r\n"
    sess.v7_mesg = args[0]
    return f"is {sess.v7_mesg}\r\n"


def _cmd_write(sess: Session, args: list[str]) -> str:
    if not args:
        return "write: usage: write user\r\n"
    if args[0] != sess.v7_user:
        return "write: user not logged on\r\n"
    if sess.v7_mesg != "y":
        return "write: permission denied\r\n"
    note = " ".join(args[1:])
    return f"Message from {sess.v7_user} on tty02\r\n{note}\r\n"


def _cmd_wall(sess: Session, args: list[str]) -> str:
    if sess.v7_uid != 0:
        return "wall: permission denied\r\n"
    return "Broadcast message from root\r\n" + " ".join(args) + "\r\n"


def _cmd_learn(sess: Session, args: list[str]) -> str:
    return "learn: not found\r\n"


def _cmd_calendar(sess: Session, args: list[str]) -> str:
    return "(empty)\r\n"


def _cmd_news(sess: Session, args: list[str]) -> str:
    path = "/usr/news/cutover"
    if _meta(sess, path) is None:
        return "news: no news\r\n"
    return _show_file(sess, path)


def _cmd_finger(sess: Session, args: list[str]) -> str:
    if not args:
        rows = [f"{row['name']:<8}{row['gecos']}" for row in _users(sess).values()]
        return "\r\n".join(rows) + "\r\n"
    row = _users(sess).get(args[0])
    if row is None:
        return f"finger: {args[0]}: no such user\r\n"
    return f"Login name: {row['name']}\r\nDirectory: {row['home']}\r\nGecos: {row['gecos']}\r\n"


def _cmd_quot(sess: Session, args: list[str]) -> str:
    return "root      9408\r\nhank       120\r\npat         40\r\nguest        2\r\n"


def _cmd_sum(sess: Session, args: list[str]) -> str:
    if not args:
        sess.v7_status = 1
        return "sum: usage: sum file\r\n"
    raw = args[0]
    path = _norm(sess.v7_cwd, raw)
    info = _meta(sess, path)
    if info is None or info.get("dir") or info.get("type") == "dir" or not _can(sess, path, False):
        sess.v7_status = 1
        return f"sum: can't open {raw}\r\n"
    if info.get("type") in {"exec", "dev"} or info.get("binary"):
        size = int(info.get("size") or 0)
        total = (size * 17) & 0xFFFF
        blocks = max(1, (size + 511) // 512)
        return f"{total:05d} {blocks:5d}\r\n"
    text = _read_bytes(sess, path) or ""
    total = sum(text.encode("utf-8", "replace")) & 0xFFFF
    blocks = max(1, (len(text) + 511) // 512)
    return f"{total:05d} {blocks:5d}\r\n"


def _cmd_sync(sess: Session, args: list[str]) -> str:
    return "sync: ok\r\n"


def _cmd_mkdir(sess: Session, args: list[str]) -> str:
    if len(args) != 1:
        return "mkdir: usage: mkdir dir\r\n"
    path = _norm(sess.v7_cwd, args[0])
    if _meta(sess, path) is not None:
        return "mkdir: already exists\r\n"
    parent = path.rsplit("/", 1)[0] or "/"
    if not _is_dir(sess, parent) or not _writable_place(sess, path):
        return "mkdir: permission denied\r\n"
    state = _load_state(sess)
    state.setdefault("meta", {})[path] = {
        "type": "dir",
        "dir": True,
        "mode": "755",
        "uid": sess.v7_uid,
        "gid": sess.v7_gid,
        "mtime": "Nov  8  1993",
        "size": 512,
        "nlink": 2,
    }
    deleted = set(state.get("deleted") or [])
    deleted.discard(path)
    state["deleted"] = sorted(deleted)
    _save_state(sess, state)
    return ""


def _cmd_rmdir(sess: Session, args: list[str]) -> str:
    if len(args) != 1:
        return "rmdir: usage: rmdir dir\r\n"
    path = _norm(sess.v7_cwd, args[0])
    if not _is_dir(sess, path):
        return "rmdir: not a directory\r\n"
    if path in _gold_table(sess.host) or not _writable_place(sess, path):
        return "rmdir: permission denied\r\n"
    if _children(sess, path):
        return "rmdir: not empty\r\n"
    state = _load_state(sess)
    state.setdefault("deleted", []).append(path)
    (state.get("meta") or {}).pop(path, None)
    _save_state(sess, state)
    return ""


def _cmd_chgrp(sess: Session, args: list[str]) -> str:
    if sess.v7_uid != 0:
        return "chgrp: permission denied\r\n"
    if len(args) != 2:
        return "chgrp: usage: chgrp group file\r\n"
    by_name, _by_id = _group_maps(sess.host)
    if args[0] not in by_name:
        return "chgrp: not found\r\n"
    path = _norm(sess.v7_cwd, args[1])
    info = _meta(sess, path)
    if info is None:
        return "chgrp: not found\r\n"
    state = _load_state(sess)
    meta = dict(info)
    meta["gid"] = by_name[args[0]]
    state.setdefault("meta", {})[path] = meta
    _save_state(sess, state)
    return ""


def _cmd_groups(sess: Session, args: list[str]) -> str:
    name = args[0] if args else sess.v7_user
    row = _users(sess).get(name)
    if row is None:
        return f"groups: {name}: no such user\r\n"
    return _group_name(sess, int(row["gid"])) + "\r\n"


def _cmd_newgrp(sess: Session, args: list[str]) -> str:
    if len(args) != 1:
        return "newgrp: usage: newgrp group\r\n"
    by_name, _by_id = _group_maps(sess.host)
    if args[0] not in by_name:
        return f"newgrp: {args[0]}: not found\r\n"
    sess.v7_gid = by_name[args[0]]
    return ""


def _cmd_sed(sess: Session, args: list[str]) -> str:
    if len(args) < 2 or not args[0].startswith("s") or len(args[0]) < 4:
        return "sed: usage: sed s/old/new/ file\r\n"
    delim = args[0][1]
    parts = args[0][2:].split(delim)
    if len(parts) < 2:
        return "sed: bad expression\r\n"
    text = _show_file(sess, args[-1])
    if text.startswith("cat:"):
        return text
    return text.replace(parts[0], parts[1])


def _cmd_comm(sess: Session, args: list[str]) -> str:
    if len(args) != 2:
        return "comm: usage: comm file1 file2\r\n"
    left = _show_file(sess, args[0])
    right = _show_file(sess, args[1])
    if left.startswith("cat:"):
        return left
    if right.startswith("cat:"):
        return right
    left_lines = [line for line in left.split("\r\n") if line != ""]
    right_lines = [line for line in right.split("\r\n") if line != ""]
    right_set = set(right_lines)
    left_set = set(left_lines)
    out = []
    for line in left_lines:
        out.append(("\t\t" + line) if line in right_set else line)
    for line in right_lines:
        if line not in left_set:
            out.append("\t" + line)
    return ("\r\n".join(out) + "\r\n") if out else ""


def _cmd_awk(sess: Session, args: list[str]) -> str:
    return "awk: not found\r\n"


def _cmd_true(sess: Session, args: list[str]) -> str:
    sess.v7_status = 0
    return ""


def _cmd_false(sess: Session, args: list[str]) -> str:
    sess.v7_status = 1
    return ""


def _cmd_kill(sess: Session, args: list[str]) -> str:
    pids = [arg for arg in args if not arg.startswith("-")]
    if not pids:
        sess.v7_status = 1
        return "kill: usage: kill pid\r\n"
    sess.v7_status = 1
    return f"kill: {pids[0]}: no such process\r\n"


def _cmd_export(sess: Session, args: list[str]) -> str:
    if not args:
        return f"PATH={sess.v7_path}\r\n"
    for arg in args:
        if "=" not in arg:
            continue
        key, value = arg.split("=", 1)
        if key == "PATH":
            sess.v7_path = value
    return ""


def _cmd_wait(sess: Session, args: list[str]) -> str:
    return ""


_COMMANDS = {
    "whoami": _cmd_whoami,
    "id": _cmd_id,
    "pwd": _cmd_pwd,
    "cd": _cmd_cd,
    "ls": _cmd_ls,
    "cat": _cmd_cat,
    "more": _cmd_more,
    "head": _cmd_head,
    "tail": _cmd_tail,
    "grep": _cmd_grep,
    "egrep": _cmd_grep,
    "fgrep": _cmd_grep,
    "echo": _cmd_echo,
    "date": _cmd_date,
    "who": _cmd_who,
    "w": _cmd_w,
    "ps": _cmd_ps,
    "su": _cmd_su,
    "passwd": _cmd_passwd,
    "chmod": _cmd_chmod,
    "chown": _cmd_chown,
    "chgrp": _cmd_chgrp,
    "mkdir": _cmd_mkdir,
    "rmdir": _cmd_rmdir,
    "touch": _cmd_touch,
    "rm": _cmd_rm,
    "cp": _cmd_cp,
    "mv": _cmd_mv,
    "mail": _cmd_mail,
    "man": _cmd_man,
    "uname": _cmd_uname,
    "hostname": _cmd_hostname,
    "stty": _cmd_stty,
    "cu": _cmd_cu,
    "connect": _cmd_cu,
    "claim": _cmd_claim,
    "useradd": _cmd_useradd,
    "adduser": _cmd_useradd,
    "logout": _cmd_logout,
    "exit": _cmd_logout,
    "bye": _cmd_logout,
    "help": _cmd_help,
    "pico": _cmd_pico,
    "ed": _cmd_ed,
    "vi": _cmd_vi,
    "wc": _cmd_wc,
    "file": _cmd_file,
    "od": _cmd_od,
    "hd": _cmd_od,
    "cmp": _cmd_cmp,
    "diff": _cmd_diff,
    "comm": _cmd_comm,
    "sed": _cmd_sed,
    "awk": _cmd_awk,
    "sort": _cmd_sort,
    "uniq": _cmd_uniq,
    "cut": _cmd_cut,
    "tr": _cmd_tr,
    "tee": _cmd_tee,
    "test": _cmd_test,
    "[": _cmd_test,
    "expr": _cmd_expr,
    "sleep": _cmd_sleep,
    "clear": _cmd_clear,
    "tput": _cmd_tput,
    "df": _cmd_df,
    "du": _cmd_du,
    "tty": _cmd_tty,
    "mesg": _cmd_mesg,
    "write": _cmd_write,
    "wall": _cmd_wall,
    "learn": _cmd_learn,
    "calendar": _cmd_calendar,
    "news": _cmd_news,
    "finger": _cmd_finger,
    "quot": _cmd_quot,
    "sum": _cmd_sum,
    "sync": _cmd_sync,
    "last": _cmd_last,
    "groups": _cmd_groups,
    "newgrp": _cmd_newgrp,
    "true": _cmd_true,
    "false": _cmd_false,
    "kill": _cmd_kill,
    "export": _cmd_export,
    "wait": _cmd_wait,
}
