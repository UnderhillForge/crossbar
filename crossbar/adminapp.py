"""Operator console. Localhost only. Not a door and not linked from the PAD."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from crossbar import site, v7
from crossbar.accounts import (
    achievements_for,
    connect as accounts_connect,
    get_account,
    hash_password,
    init_db,
    list_accounts,
    revoke_achievement,
    set_account_fields,
)
from crossbar.operators import (
    audit,
    authenticate,
    create_operator,
    csrf_ok,
    current_session,
    drop_session,
    issue_setup_token,
    open_session,
    operator_count,
    read_audit,
    setup_open,
)
from crossbar.packs import PACKS
from crossbar.session import SESSIONS, publish_sessions

_COOKIE = "crossbar_op"
_ROOT = Path(__file__).resolve().parent.parent
_FAILS: dict[str, list[float]] = {}
_NAV = (
    ("overview", "Overview"),
    ("sessions", "Sessions"),
    ("accounts", "Accounts"),
    ("hosts", "Hosts"),
    ("flags", "Flags"),
    ("content", "Content"),
    ("verbs", "Verbs"),
    ("chain", "Chain"),
    ("mail", "Mail"),
    ("audit", "Audit"),
    ("config", "Config"),
)


def _esc(value: object) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _client_ip(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def _limited(ip: str) -> bool:
    now = time.monotonic()
    hits = [stamp for stamp in _FAILS.get(ip, []) if now - stamp < 900]
    _FAILS[ip] = hits
    return len(hits) >= 8


def _mark_fail(ip: str) -> None:
    _FAILS.setdefault(ip, []).append(time.monotonic())


def _clear_fail(ip: str) -> None:
    _FAILS.pop(ip, None)


def _token(request: Request) -> str:
    return request.cookies.get(_COOKIE, "")


def _who(request: Request) -> tuple[str, str, str] | None:
    return current_session(_token(request))


def _secure(request: Request) -> bool:
    if os.environ.get("CROSSBAR_ADMIN_SECURE", "").strip() == "1":
        return True
    return request.url.scheme == "https"


def _page(title: str, body: str, who: tuple[str, str, str] | None) -> str:
    links = []
    for slug, label in _NAV:
        links.append(f'<a href="/o/{slug}">{label}</a>')
    nav = "\n".join(links)
    user = ""
    if who:
        user = f'<p class="who">{_esc(who[0])} · {_esc(who[1])}</p><form method="post" action="/o/logout"><button>logout</button></form>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body {{ margin:0; background:#111; color:#c8c8c8; font:13px/1.4 ui-monospace,monospace; }}
nav {{ position:fixed; inset:0 auto 0 0; width:11rem; background:#1b1b1b; padding:12px; }}
nav a {{ display:block; color:#ddd; text-decoration:none; padding:3px 0; }}
main {{ margin-left:12rem; padding:16px 20px 40px; max-width:70rem; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ border-bottom:1px solid #333; text-align:left; padding:4px 8px; vertical-align:top; }}
input, textarea, select, button {{ background:#1c1c1c; color:#ddd; border:1px solid #444; padding:3px 6px; }}
textarea {{ width:100%; min-height:8rem; }}
pre {{ background:#0c0c0c; padding:8px; overflow:auto; }}
.who {{ color:#888; }}
</style></head><body>
<nav><strong>CROSSBAR</strong>{user}{nav}</nav>
<main><h1>{_esc(title)}</h1>
{body}
</main></body></html>"""


def _csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'


def _deny() -> Response:
    return PlainTextResponse("401\n", status_code=401)


def _need(request: Request, write: bool = False) -> tuple[str, str, str] | Response:
    who = _who(request)
    if who is None:
        return _deny()
    if write and who[1] != "operator":
        return PlainTextResponse("403\n", status_code=403)
    return who


def _form_ok(request: Request, form, who: tuple[str, str, str]) -> bool:
    return csrf_ok(who[2], str(form.get("csrf") or ""))


async def root(request: Request) -> Response:
    return _deny()


async def login_get(request: Request) -> Response:
    if _who(request):
        return RedirectResponse("/o/overview", status_code=303)
    body = """<form method="post" action="/o/login">
<p><label>operator <input name="name" autocomplete="username"></label></p>
<p><label>password <input name="password" type="password" autocomplete="current-password"></label></p>
<button>enter</button></form>"""
    if setup_open():
        body += '<p><a href="/o/setup">first operator</a></p>'
    return HTMLResponse(_page("Login", body, None))


async def login_post(request: Request) -> Response:
    ip = _client_ip(request)
    if _limited(ip):
        return PlainTextResponse("429\n", status_code=429)
    form = await request.form()
    name = str(form.get("name") or "")
    password = str(form.get("password") or "")
    role = authenticate(name, password)
    if role is None:
        _mark_fail(ip)
        audit("", "login-fail", name.strip().lower(), ip)
        return HTMLResponse(_page("Login", "<p>refused</p>", None), status_code=401)
    _clear_fail(ip)
    token, _csrf = open_session(name)
    audit(name.strip().lower(), "login", role, ip)
    response = RedirectResponse("/o/overview", status_code=303)
    response.set_cookie(
        _COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=_secure(request),
        path="/",
        max_age=15 * 60,
    )
    return response


async def logout(request: Request) -> Response:
    drop_session(_token(request))
    response = RedirectResponse("/o/login", status_code=303)
    response.delete_cookie(_COOKIE, path="/")
    return response


async def setup_get(request: Request) -> Response:
    if not setup_open():
        return _deny()
    body = """<form method="post" action="/o/setup">
<p><label>token <input name="token"></label></p>
<p><label>operator <input name="name"></label></p>
<p><label>password <input name="password" type="password"></label></p>
<button>create</button></form>
<p>Password at least 10 characters. Names admin, sysop, and root are refused.</p>"""
    return HTMLResponse(_page("Bootstrap", body, None))


async def setup_post(request: Request) -> Response:
    if operator_count():
        return _deny()
    form = await request.form()
    try:
        create_operator(
            str(form.get("name") or ""),
            str(form.get("password") or ""),
            "operator",
            token=str(form.get("token") or ""),
        )
    except ValueError:
        return HTMLResponse(_page("Bootstrap", "<p>refused</p>", None), status_code=400)
    audit(str(form.get("name") or "").strip().lower(), "bootstrap", "first operator", _client_ip(request))
    return RedirectResponse("/o/login", status_code=303)


async def overview(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    init_db()
    publish_sessions()
    accounts = list_accounts()
    guests = 0
    named = 0
    for row in _session_rows():
        if (row.get("handle") or "").lower() == "guest" or not row.get("handle"):
            guests += 1
        else:
            named += 1
    hosts = "".join(
        f"<tr><td>{_esc(label)}</td><td>{_esc(state)}</td></tr>"
        for label, _title, _baud, state in site.pad_host_rows()
    )
    stamps = accounts_connect().execute(
        "SELECT owner, code, earned FROM achievements ORDER BY earned DESC LIMIT 8"
    ).fetchall()
    claim_rows = "".join(
        f"<tr><td>{_esc(row['code'])}</td><td>{_esc(row['owner'])}</td><td>{_esc(row['earned'])}</td></tr>"
        for row in stamps
    )
    packs = _dir_bytes(_ROOT / "packs")
    overlays = _dir_bytes(_ROOT / "data" / "users")
    body = f"""<table>
<tr><th>sessions</th><td>{named} registered on glass / {guests} guest or empty</td></tr>
<tr><th>accounts</th><td>{len(accounts)}</td></tr>
<tr><th>rpc</th><td>rpc down</td></tr>
<tr><th>packs bytes</th><td>{packs}</td></tr>
<tr><th>overlay bytes</th><td>{overlays}</td></tr>
</table>
<h2>hosts</h2><table>{hosts}</table>
<h2>last stamps</h2><table>{claim_rows or "<tr><td>none</td></tr>"}</table>"""
    return HTMLResponse(_page("Overview", body, who))


def _session_rows() -> list[dict]:
    path = site.site_dir() / "sessions.json"
    if not path.is_file():
        publish_sessions()
    if not path.is_file():
        return []
    try:
        data = json_load(path)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def json_load(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _dir_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _mask(row: dict) -> str:
    digest = str(row.get("ip_hash") or "")
    return digest or "—"


async def sessions_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    publish_sessions()
    rows = []
    for row in _session_rows():
        rows.append(
            "<tr>"
            f"<td>{_esc(row.get('handle') or '—')}</td>"
            f"<td>{_esc(row.get('dest'))}</td>"
            f"<td>{_esc(row.get('baud'))}</td>"
            f"<td>{_esc(row.get('idle'))}</td>"
            f"<td>{_esc(_mask(row))}</td>"
            f"<td><form method='post' action='/o/sessions'>{_csrf_field(who[2])}"
            f"<input type='hidden' name='sid' value='{_esc(row.get('sid'))}'>"
            "<button>kill</button></form></td></tr>"
        )
    body = "<table><tr><th>handle</th><th>dest</th><th>baud</th><th>idle</th><th>ip hash</th><th></th></tr>"
    body += "".join(rows) or "<tr><td colspan='6'>none</td></tr>"
    body += "</table>"
    return HTMLResponse(_page("Sessions", body, who))


async def sessions_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    sid = str(form.get("sid") or "")
    ip = ""
    for row in _session_rows():
        if row.get("sid") == sid:
            ip = str(row.get("ip") or "")
    _queue_kill(sid)
    sess = SESSIONS.pop(sid, None)
    if sess is not None and sess.socket is not None:
        sess.user = None
    audit(who[0], "kill-session", f"sid={sid[:8]} ip={ip}", _client_ip(request))
    publish_sessions()
    return RedirectResponse("/o/sessions", status_code=303)


def _queue_kill(sid: str) -> None:
    import json

    path = site.site_dir() / "kills.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current: list[str] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                current = [str(item) for item in loaded]
        except (OSError, json.JSONDecodeError):
            current = []
    current.append(sid)
    path.write_text(json.dumps(current), encoding="utf-8")


async def accounts_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    init_db()
    rows = []
    for account in list_accounts():
        grants = " ".join(achievements_for(f"acct:{account['handle']}")) or "—"
        overlay = _dir_bytes(_ROOT / "data" / "users" / account["handle"] / "bec")
        rows.append(
            "<tr>"
            f"<td>{_esc(account['handle'])}</td>"
            f"<td>{_esc(account['created'])}</td>"
            f"<td>{_esc(account['last_login'] or '—')}</td>"
            f"<td>{_esc(account['status'])}{' must-change' if account['must_change'] else ''}</td>"
            f"<td>{_esc(account['wallet'] or '—')}</td>"
            f"<td>{_esc(grants)}</td>"
            f"<td>{overlay}</td>"
            f"<td>{_esc(account['note'])}</td>"
            "</tr>"
        )
    notice = _esc(request.query_params.get("temp") or "")
    extra = f"<p>temporary password (shown once): {notice}</p>" if notice else ""
    body = extra + "<table><tr><th>handle</th><th>created</th><th>last</th><th>status</th><th>wallet</th><th>grants</th><th>overlay</th><th>note</th></tr>"
    body += "".join(rows) or "<tr><td>none</td></tr>"
    body += "</table>"
    body += f"""<h2>action</h2>
<form method="post" action="/o/accounts">{_csrf_field(who[2])}
<p><input name="handle" placeholder="handle"></p>
<p><select name="action">
<option value="disable">disable</option>
<option value="enable">enable</option>
<option value="reset">reset password</option>
<option value="unbind">unbind wallet</option>
<option value="wipe">wipe bec overlay</option>
<option value="rotate">rotate overlay token</option>
<option value="note">note</option>
</select></p>
<p><input name="note" placeholder="note or reason"></p>
<button>apply</button></form>"""
    return HTMLResponse(_page("Accounts", body, who))


async def accounts_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    handle = str(form.get("handle") or "").strip().lower()
    action = str(form.get("action") or "")
    note = str(form.get("note") or "")
    if get_account(handle) is None:
        return HTMLResponse(_page("Accounts", "<p>no such handle</p>", who), status_code=404)
    temp = ""
    if action == "disable":
        set_account_fields(handle, status="disabled")
    elif action == "enable":
        set_account_fields(handle, status="ok")
    elif action == "reset":
        temp = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
        set_account_fields(handle, password_hash=hash_password(temp), must_change=1)
    elif action == "unbind":
        set_account_fields(handle, wallet="")
    elif action == "wipe":
        folder = _ROOT / "data" / "users" / handle / "bec"
        if folder.is_dir():
            shutil.rmtree(folder)
    elif action == "rotate":
        _rotate_token(handle)
    elif action == "note":
        set_account_fields(handle, note=note)
    else:
        return PlainTextResponse("400\n", status_code=400)
    audit(who[0], f"account-{action}", f"{handle} {note}".strip(), _client_ip(request))
    target = "/o/accounts"
    if temp:
        target += f"?temp={temp}"
    return RedirectResponse(target, status_code=303)


def _rotate_token(handle: str) -> None:
    import json

    folder = _ROOT / "data" / "users" / handle / "bec"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "state.json"
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["_admin_token"] = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


async def hosts_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    rows = []
    for name, pack in PACKS.items():
        state = site.host_state(name, pack.up, pack.up)
        rows.append(
            "<tr>"
            f"<td>{_esc(name)}</td>"
            f"<td>{_esc(pack.here_line)}</td>"
            f"<td>{pack.baud_max}</td>"
            f"<td>{_esc(pack.era or '—')}</td>"
            f"<td>{_esc(state)}</td></tr>"
        )
    glass = "<pre>" + _esc(site_hosts_text()) + "</pre>"
    body = (
        "<table><tr><th>id</th><th>title</th><th>baud_max</th><th>era</th><th>state</th></tr>"
        + "".join(rows)
        + "</table><h2>PAD HOSTS</h2>"
        + glass
        + f"""<form method="post" action="/o/hosts">{_csrf_field(who[2])}
<p><input name="name" placeholder="bec or terminal-addiction"></p>
<p><input name="title" placeholder="HOSTS title (optional)"></p>
<p><select name="state"><option>UP</option><option>OFFLINE</option><option>MAINT</option></select></p>
<button>save</button></form>
<form method="post" action="/o/hosts">{_csrf_field(who[2])}
<input type="hidden" name="reload" value="1"><button>reload gold from disk</button></form>"""
    )
    return HTMLResponse(_page("Hosts", body, who))


def site_hosts_text() -> str:
    from crossbar.lobby import pad_hosts_text

    return pad_hosts_text()


async def hosts_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    if str(form.get("reload") or "") == "1":
        site.bump_gold()
        v7._GOLD.clear()
        audit(who[0], "reload-gold", "packs reread; overlays kept", _client_ip(request))
        return RedirectResponse("/o/hosts", status_code=303)
    name = str(form.get("name") or "").strip().lower()
    state = str(form.get("state") or "").upper()
    title = str(form.get("title") or "")
    if name not in {"bec", "terminal-addiction", "tymnet"} or state not in {"UP", "OFFLINE", "MAINT"}:
        return PlainTextResponse("400\n", status_code=400)
    site.set_host(name, state, title or None)
    audit(who[0], "host-state", f"{name} {state} {title}".strip(), _client_ip(request))
    return RedirectResponse("/o/hosts", status_code=303)


def _flag_catalog() -> list[dict]:
    path = _ROOT / "packs" / "big-evil" / "flags.yaml"
    if not path.is_file():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [row for row in loaded if isinstance(row, dict)]


async def flags_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    init_db()
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for row in accounts_connect().execute(
        "SELECT code, owner, earned FROM achievements ORDER BY earned"
    ):
        counts[row["code"]] = counts.get(row["code"], 0) + 1
        first.setdefault(row["code"], f"{row['owner']} {row['earned']}")
    rows = []
    for flag in _flag_catalog():
        code = str(flag.get("id") or "")
        rows.append(
            "<tr>"
            f"<td>{_esc(code)}</td><td>big-evil</td>"
            f"<td>{_esc(flag.get('trigger'))}</td>"
            f"<td>{counts.get(code, 0)}</td>"
            f"<td>{_esc(first.get(code, '—'))}</td></tr>"
        )
    body = (
        "<table><tr><th>id</th><th>pack</th><th>trigger</th><th>stamped</th><th>first</th></tr>"
        + "".join(rows)
        + "</table>"
        + f"""<form method="post" action="/o/flags">{_csrf_field(who[2])}
<p><input name="handle" placeholder="handle"></p>
<p><input name="code" placeholder="BEC-MF"></p>
<p><input name="reason" placeholder="reason (required)"></p>
<p><select name="action"><option value="grant">grant</option><option value="revoke">revoke</option></select></p>
<button>apply</button></form>"""
    )
    return HTMLResponse(_page("Flags", body, who))


async def flags_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    handle = str(form.get("handle") or "").strip().lower()
    code = str(form.get("code") or "").strip()
    reason = str(form.get("reason") or "").strip()
    action = str(form.get("action") or "")
    known = {str(row.get("id")) for row in _flag_catalog()}
    if not reason or code not in known or get_account(handle) is None:
        return HTMLResponse(_page("Flags", "<p>reason, known flag, and account required</p>", who), status_code=400)
    owner = f"acct:{handle}"
    if action == "grant":
        from crossbar.accounts import grant_achievement

        grant_achievement(owner, code)
    elif action == "revoke":
        revoke_achievement(owner, code)
    else:
        return PlainTextResponse("400\n", status_code=400)
    audit(who[0], f"flag-{action}", f"{code} {handle} {reason}", _client_ip(request))
    return RedirectResponse("/o/flags", status_code=303)


async def content_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    from crossbar.lobby import help_text, motd_text

    motd = site.motd_override() if site.motd_override() is not None else motd_text()
    help_body = site.help_override() if site.help_override() is not None else help_text()
    allow = "\n".join(site.news_allow())
    files = []
    packs = _ROOT / "packs"
    for path in sorted(packs.rglob("*")):
        if path.suffix.lower() in {".txt", ".ans", ".md"} and path.is_file() and "SECRETS" not in path.name:
            files.append(path.relative_to(_ROOT).as_posix())
    listing = "".join(f"<li><a href='/o/content?file={_esc(name)}'>{_esc(name)}</a></li>" for name in files[:80])
    preview = ""
    chosen = request.query_params.get("file") or ""
    if chosen:
        text = _safe_text(chosen)
        preview = f"<h2>{_esc(chosen)}</h2><pre>{_ansi_html(text)}</pre>"
    body = f"""<form method="post" action="/o/content">{_csrf_field(who[2])}
<p>MOTD</p><textarea name="motd">{_esc(motd)}</textarea>
<p>PAD help</p><textarea name="help">{_esc(help_body)}</textarea>
<p>NEWS allowlist (one host per line)</p><textarea name="allow">{_esc(allow)}</textarea>
<button>save override</button></form>
<h2>pack text</h2><ul>{listing}</ul>{preview}"""
    return HTMLResponse(_page("Content", body, who))


def _safe_text(rel: str) -> str:
    path = (_ROOT / rel).resolve()
    root = _ROOT.resolve()
    if root not in path.parents or not path.is_file():
        return ""
    if path.suffix.lower() not in {".txt", ".ans", ".md", ".yaml"}:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _ansi_html(text: str) -> str:
    """Render a few SGR colors. Never executes the file."""
    import re

    out = []
    color = ""
    parts = re.split(r"\x1b\[([0-9;]*)m", text)
    # split keeps the codes in odd indexes when used this way? 
    # re.split with a group returns [text, code, text, code...]
    index = 0
    while index < len(parts):
        chunk = parts[index]
        if index % 2 == 1:
            color = "color:#9cf" if "36" in chunk else ""
            if chunk in {"0", ""}:
                color = ""
        else:
            escaped = _esc(chunk)
            if color:
                out.append(f'<span style="{color}">{escaped}</span>')
            else:
                out.append(escaped)
        index += 1
    return "".join(out)


async def content_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    site.set_text("motd", str(form.get("motd") or ""))
    site.set_text("help", str(form.get("help") or ""))
    allow = [line.strip() for line in str(form.get("allow") or "").splitlines() if line.strip()]
    site.set_news_allow(allow)
    audit(who[0], "content", f"motd/help/allow {len(allow)}", _client_ip(request))
    return RedirectResponse("/o/content", status_code=303)


async def verbs_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    boxes = []
    for name in ("news", "map", "finger"):
        checked = "checked" if site.verb_enabled(name) else ""
        boxes.append(
            f"<label><input type='checkbox' name='{name}' {checked}> {name.upper()}</label><br>"
        )
    body = f"<form method='post' action='/o/verbs'>{_csrf_field(who[2])}{''.join(boxes)}<button>save</button></form>"
    return HTMLResponse(_page("Verbs", body, who))


async def verbs_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    for name in ("news", "map", "finger"):
        site.set_verb(name, name in form)
    audit(who[0], "verbs", " ".join(name for name in ("news", "map", "finger") if name in form), _client_ip(request))
    return RedirectResponse("/o/verbs", status_code=303)


async def chain_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    body = f"""<p>hub address (no key): {_esc(site.hub_address() or "—")}</p>
<p>last error: rpc down</p>
<form method="post" action="/o/chain">{_csrf_field(who[2])}
<p><input name="address" placeholder="address only" value="{_esc(site.hub_address())}"></p>
<button name="op" value="save">save address</button>
<button name="op" value="dry">dry-run</button>
<button name="op" value="queue">queue daily report</button>
</form>"""
    return HTMLResponse(_page("Chain", body, who))


async def chain_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    op = str(form.get("op") or "")
    if op == "save":
        site.set_hub_address(str(form.get("address") or ""))
        audit(who[0], "hub-address", "updated", _client_ip(request))
    elif op == "dry":
        audit(who[0], "report-dry-run", "rpc down", _client_ip(request))
    elif op == "queue":
        site.queue_report(f"{who[0]} queued daily report")
        audit(who[0], "report-queue", "queued", _client_ip(request))
    else:
        return PlainTextResponse("400\n", status_code=400)
    return RedirectResponse("/o/chain", status_code=303)


async def mail_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    from crossbar import mail

    flash = request.query_params.get("flash") or ""
    flash_html = f"<p><strong>{_esc(flash)}</strong></p>" if flash else ""
    conn = accounts_connect()
    totals = conn.execute("SELECT COUNT(*) AS n FROM mail_messages").fetchone()
    unread = conn.execute(
        "SELECT COUNT(*) AS n FROM mail_copies "
        "WHERE deleted = 0 AND folder = 'inbox' AND read_at IS NULL"
    ).fetchone()
    last = conn.execute(
        "SELECT created, subject FROM mail_messages WHERE kind = 'broadcast' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_line = "—"
    if last is not None:
        last_line = f"{_esc(last['created'])} · {_esc(last['subject'] or '(no subject)')}"
    stats = (
        f"<p>messages: {_esc(totals['n'] if totals else 0)} · "
        f"unread inbox copies: {_esc(unread['n'] if unread else 0)}</p>"
        f"<p>last broadcast: {last_line}</p>"
    )
    form = f"""{flash_html}{stats}
<form method="post" action="/o/mail">{_csrf_field(who[2])}
<p>Subject <input name="subject" maxlength="60" style="width:28rem"></p>
<p>Body</p><textarea name="body" maxlength="4000"></textarea>
<p>
<label><input type="radio" name="audience" value="all" checked> all ok accounts</label><br>
<label><input type="radio" name="audience" value="handles"> handles
<input name="handles" placeholder="ada bob carol" style="width:20rem"></label>
</p>
<button>broadcast as sysop</button></form>
<p class="who">From: sysop · pad-local only · NNTP upstream: not configured</p>"""
    return HTMLResponse(_page("Mail", form, who))


async def mail_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    from crossbar import mail

    subject = str(form.get("subject") or "")
    body = str(form.get("body") or "")
    audience = str(form.get("audience") or "all").strip().lower()
    handles_raw = str(form.get("handles") or "")
    handles: list[str] | None = None
    if audience == "handles":
        handles = [
            part.strip().lower()
            for part in handles_raw.replace(",", " ").split()
            if part.strip()
        ]
    try:
        sent, skipped = mail.broadcast(subject=subject, body=body, handles=handles)
    except mail.NoRecipientsError as exc:
        flash = f"no recipients (skipped {exc.skipped})"
        return RedirectResponse(f"/o/mail?flash={quote(flash)}", status_code=303)
    except ValueError as exc:
        return RedirectResponse(f"/o/mail?flash={quote(str(exc))}", status_code=303)
    detail = f"n={sent} skipped={skipped} {(subject or '(no subject)')[:40]}"
    audit(who[0], "mail-broadcast", detail, _client_ip(request))
    flash = f"sent {sent}, skipped {skipped}"
    return RedirectResponse(f"/o/mail?flash={quote(flash)}", status_code=303)


async def audit_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    op = request.query_params.get("operator") or ""
    action = request.query_params.get("action") or ""
    handle = request.query_params.get("handle") or ""
    rows = []
    for item in read_audit():
        blob = " ".join(str(item.get(key) or "") for key in ("operator", "action", "detail"))
        if op and op not in str(item.get("operator") or ""):
            continue
        if action and action not in str(item.get("action") or ""):
            continue
        if handle and handle not in blob:
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(item.get('ts'))}</td>"
            f"<td>{_esc(item.get('operator'))}</td>"
            f"<td>{_esc(item.get('action'))}</td>"
            f"<td>{_esc(item.get('detail'))}</td>"
            f"<td>{_esc(item.get('ip'))}</td></tr>"
        )
    body = f"""<form method="get" action="/o/audit">
<input name="operator" placeholder="operator" value="{_esc(op)}">
<input name="action" placeholder="action" value="{_esc(action)}">
<input name="handle" placeholder="handle" value="{_esc(handle)}">
<button>filter</button></form>
<table><tr><th>when</th><th>operator</th><th>action</th><th>detail</th><th>ip</th></tr>
{''.join(rows) or '<tr><td>none</td></tr>'}</table>"""
    return HTMLResponse(_page("Audit", body, who))


async def config_get(request: Request) -> Response:
    who = _need(request)
    if isinstance(who, Response):
        return who
    from crossbar.config import HOST, IDLE_TTL, PORT

    guest = "checked" if site.guest_enabled() else ""
    reg = "checked" if site.registration_enabled() else ""
    baud = "".join(
        f"<tr><td>{_esc(name)}</td><td>{pack.baud_max}</td><td>{_esc(pack.era or '—')}</td></tr>"
        for name, pack in PACKS.items()
    )
    body = f"""<p>public bind {HOST}:{PORT}. admin bind is this process. A bind change needs a restart.</p>
<p>player idle timeout {IDLE_TTL}s. operator idle timeout 900s.</p>
<form method="post" action="/o/config">{_csrf_field(who[2])}
<label><input type="checkbox" name="guest" {guest}> guest logon</label><br>
<label><input type="checkbox" name="registration" {reg}> registration</label><br>
<button>save</button></form>
<h2>add operator</h2>
<form method="post" action="/o/config">{_csrf_field(who[2])}
<input type="hidden" name="add" value="1">
<input name="name" placeholder="name">
<input name="password" type="password" placeholder="password">
<select name="role"><option>watch</option><option>operator</option></select>
<button>add</button></form>
<h2>baud</h2><table><tr><th>pack</th><th>baud_max</th><th>era</th></tr>{baud}</table>"""
    return HTMLResponse(_page("Config", body, who))


async def config_post(request: Request) -> Response:
    who = _need(request, write=True)
    if isinstance(who, Response):
        return who
    form = await request.form()
    if not _form_ok(request, form, who):
        return PlainTextResponse("403\n", status_code=403)
    if str(form.get("add") or "") == "1":
        try:
            create_operator(str(form.get("name") or ""), str(form.get("password") or ""), str(form.get("role") or "watch"))
        except ValueError:
            return HTMLResponse(_page("Config", "<p>refused</p>", who), status_code=400)
        audit(who[0], "operator-add", str(form.get("name") or ""), _client_ip(request))
        return RedirectResponse("/o/config", status_code=303)
    site.set_config_flag("guest", "guest" in form)
    site.set_config_flag("registration", "registration" in form)
    audit(who[0], "config", f"guest={'guest' in form} registration={'registration' in form}", _client_ip(request))
    return RedirectResponse("/o/config", status_code=303)


def build_admin_app() -> Starlette:
    routes = [
        Route("/", root, methods=["GET"]),
        Route("/o/login", login_get, methods=["GET"]),
        Route("/o/login", login_post, methods=["POST"]),
        Route("/o/logout", logout, methods=["POST"]),
        Route("/o/setup", setup_get, methods=["GET"]),
        Route("/o/setup", setup_post, methods=["POST"]),
        Route("/o/overview", overview, methods=["GET"]),
        Route("/o/sessions", sessions_get, methods=["GET"]),
        Route("/o/sessions", sessions_post, methods=["POST"]),
        Route("/o/accounts", accounts_get, methods=["GET"]),
        Route("/o/accounts", accounts_post, methods=["POST"]),
        Route("/o/hosts", hosts_get, methods=["GET"]),
        Route("/o/hosts", hosts_post, methods=["POST"]),
        Route("/o/flags", flags_get, methods=["GET"]),
        Route("/o/flags", flags_post, methods=["POST"]),
        Route("/o/content", content_get, methods=["GET"]),
        Route("/o/content", content_post, methods=["POST"]),
        Route("/o/verbs", verbs_get, methods=["GET"]),
        Route("/o/verbs", verbs_post, methods=["POST"]),
        Route("/o/chain", chain_get, methods=["GET"]),
        Route("/o/chain", chain_post, methods=["POST"]),
        Route("/o/mail", mail_get, methods=["GET"]),
        Route("/o/mail", mail_post, methods=["POST"]),
        Route("/o/audit", audit_get, methods=["GET"]),
        Route("/o/config", config_get, methods=["GET"]),
        Route("/o/config", config_post, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def serve_admin() -> None:
    import sys

    host = os.environ.get("CROSSBAR_ADMIN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::", ""}:
        sys.stderr.write("admin bind refused. use 127.0.0.1 or a private address.\n")
        raise SystemExit(2)
    port = int(os.environ.get("CROSSBAR_ADMIN_PORT", "8081"))
    init_db()
    token = issue_setup_token()
    if token:
        sys.stdout.write(f"crossbar admin bootstrap token: {token}\n")
        sys.stdout.write("open http://127.0.0.1:%s/o/setup once. the token is not shown again.\n" % port)
        sys.stdout.flush()
    import uvicorn

    uvicorn.run(build_admin_app(), host=host, port=port, log_level="info")
