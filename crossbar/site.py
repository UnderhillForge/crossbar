"""PAD runtime overrides. Doors stay in their packs; this file is the operator layer.

Missing file means the shipped defaults. The public terminal and the admin
process both read it, so a host toggle does not need a code deploy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT_HOSTS = (
    ("bec", "BEC", "ORIENTATION  •  BEC OUTSIDE PLANT", "1200", "UP"),
    ("terminal-addiction", "TA", "TERMINAL ADDICTION", "2400", "OFFLINE"),
    ("tymnet", "TYMNET", "CARRIER HOP", "T1", "UP"),
)


def site_dir() -> Path:
    override = os.environ.get("CROSSBAR_SITE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "site"


def _runtime_path() -> Path:
    return site_dir() / "runtime.json"


def load() -> dict:
    path = _runtime_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    path = _runtime_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _hosts(data: dict) -> dict:
    found = data.get("hosts")
    return found if isinstance(found, dict) else {}


def host_state(name: str, pack_up: bool = True, listed_up: bool = True) -> str:
    row = _hosts(load()).get(name) or {}
    state = str(row.get("state") or "").upper()
    if state in {"UP", "OFFLINE", "MAINT"}:
        return state
    for key, _label, _title, _baud, default in _DEFAULT_HOSTS:
        if key == name:
            return default
    return "UP" if pack_up and listed_up else "OFFLINE"


def host_title(name: str, fallback: str) -> str:
    row = _hosts(load()).get(name) or {}
    title = str(row.get("title") or "").strip()
    return title or fallback


def set_host(name: str, state: str, title: str | None = None) -> None:
    data = load()
    hosts = _hosts(data)
    row = dict(hosts.get(name) or {})
    row["state"] = state.upper()
    if title is not None and title.strip():
        row["title"] = title.strip()
    hosts[name] = row
    data["hosts"] = hosts
    save(data)


def pad_host_rows() -> list[tuple[str, str, str, str]]:
    """(label, title, baud, state) for the GL> HOSTS table."""
    rows = []
    for key, label, title, baud, _default in _DEFAULT_HOSTS:
        rows.append((label, host_title(key, title), baud, host_state(key)))
    return rows


def verb_enabled(name: str) -> bool:
    verbs = load().get("verbs")
    if not isinstance(verbs, dict) or name not in verbs:
        return True
    return bool(verbs.get(name))


def set_verb(name: str, enabled: bool) -> None:
    data = load()
    verbs = data.get("verbs")
    if not isinstance(verbs, dict):
        verbs = {}
    verbs[name] = bool(enabled)
    data["verbs"] = verbs
    save(data)


def motd_override() -> str | None:
    text = load().get("motd")
    return text if isinstance(text, str) else None


def help_override() -> str | None:
    text = load().get("help")
    return text if isinstance(text, str) else None


def news_override() -> str | None:
    text = load().get("news")
    return text if isinstance(text, str) else None


def set_text(key: str, value: str) -> None:
    data = load()
    if value.strip():
        data[key] = value.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        data.pop(key, None)
    save(data)


def news_allow() -> list[str]:
    raw = load().get("news_allow")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def set_news_allow(lines: list[str]) -> None:
    data = load()
    data["news_allow"] = lines
    save(data)


def guest_enabled() -> bool:
    config = load().get("config")
    if not isinstance(config, dict) or "guest" not in config:
        return True
    return bool(config.get("guest"))


def registration_enabled() -> bool:
    config = load().get("config")
    if not isinstance(config, dict) or "registration" not in config:
        return True
    return bool(config.get("registration"))


def set_config_flag(name: str, enabled: bool) -> None:
    data = load()
    config = data.get("config")
    if not isinstance(config, dict):
        config = {}
    config[name] = bool(enabled)
    data["config"] = config
    save(data)


def hub_address() -> str:
    config = load().get("config")
    if not isinstance(config, dict):
        return ""
    return str(config.get("hub_address") or "")


def set_hub_address(value: str) -> None:
    data = load()
    config = data.get("config")
    if not isinstance(config, dict):
        config = {}
    config["hub_address"] = value.strip()
    data["config"] = config
    save(data)


def gold_generation() -> str:
    return str(load().get("gold_gen") or "")


def bump_gold() -> None:
    data = load()
    data["gold_gen"] = str(int(data.get("gold_gen") or "0") + 1) if str(data.get("gold_gen") or "0").isdigit() else "1"
    save(data)


def queue_report(note: str) -> Path:
    folder = site_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "queue.txt"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(note.rstrip() + "\n")
    return path
