# Operator console

The public terminal is `127.0.0.1:8080`. The operator console is a different process and a different port. Nothing on :8080 links to it. Do not hang it off `GL>` or `CONNECT`.

**Never publish 8081.** Do not put it in nginx, Cloudflare, or a public firewall rule. Players will try `/admin`. That path is not the console.

## Bind

Default: `127.0.0.1:8081`.

`0.0.0.0` is refused. A Tailscale or other private address is allowed by setting `CROSSBAR_ADMIN_HOST` to that address. Restart to change the bind.

```
python -m crossbar --admin
```

The public pad stays:

```
python -m crossbar
```

## Tunnel

From your laptop, reach a console that is bound on the server loopback:

```
ssh -L 8081:127.0.0.1:8081 user@host
```

Then open `http://127.0.0.1:8081/o/login`. The cookie is HttpOnly and SameSite=Strict. Browsers drop a `Secure` cookie on plain HTTP, so `Secure` is set only when the request is HTTPS or `CROSSBAR_ADMIN_SECURE=1`. Terminate TLS in front of the tunnel if you need the Secure flag. Cloudflare Access can sit on that private name later. It is not required.

## First operator

There is no `admin` / `admin` and no `sysop` password.

If no operators exist, `python -m crossbar --admin` prints a one-time bootstrap token on stdout. It is not shown on the pad.

```
python -m crossbar admin bootstrap
```

Open `http://127.0.0.1:8081/o/setup`, enter the token, a name that is not `admin`, `sysop`, or `root`, and a password of at least 10 characters. The token dies after use or after 60 minutes. Operator sessions idle out after 15 minutes.

A second account can be `watch` (read-only) or `operator` from Config. Watch cannot POST changes.

## What it can do

Host state (UP, OFFLINE, MAINT) is stored in `data/site/runtime.json`. The pad HOSTS table and `CONNECT` read that file, so Terminal Addiction can be turned off without a deploy. Gold trees reload from disk; player overlays are not copied over.

MOTD and the pad `?` text can be overridden in the same file. Pack text stays readable in Content. Edits land as overrides unless you are looking at a preview.

Granting a flag requires a reason and writes an audit line. Revoke removes the local stamp. The trigger text comes from `packs/big-evil/flags.yaml`.

Audit is `data/admin-audit.log`, append-only. The console can filter it. It cannot edit it.

The console may read localhost pisecured the same way the pad does. It does not proxy that RPC into the browser. Chain shows the hub address only, never a key, and the report buttons are a dry-run or a local queue.

Mail broadcasts to pad accounts as From `sysop` (pad-local letters, not SMTP). Audience is all `ok` accounts or an explicit handle list. Empty audience does not insert a message. Success writes an audit line `mail-broadcast`.

WALL posts live in `data/text/wall.asc` (not SQLite). `WALL <text>` appends one line; the pad shows the last 10. Edit or trim that file over SSH when needed. Line format: `<UTC ISO8601> <handle> <text>`.

GROUPS are local forums in `grayline.db` (not `news.asc`). Seed groups: `grayline.general`, `grayline.doors`, `grayline.sysop`. Pad POST is for registered handles; `grayline.sysop` is read-only on the pad. Packet peering and NNTP uplink are not wired yet — see `docs/design-groups.md`.

## MudProto door

`CONNECT MUDPROTO` opens one TCP connection to MudProto and pipes the xterm through it. MudProto is a **separate process** — start it before players dial.

```bash
MUDPROTO_BIND=127.0.0.1 MUDPROTO_PORT=4000 python mudproto_server/core_logic/server.py
```

Crossbar env (defaults shown):

| Variable | Default | Notes |
| --- | --- | --- |
| `MUDPROTO_HOST` | `127.0.0.1` | Door address Grayline dials |
| `MUDPROTO_PORT` | `4000` | Must stay loopback / private. Do not publish. TLS stays on Grayline. |

Health check (ops):

```python
from crossbar import mudproto
ok, banner = mudproto.health()
```

Expect a banner containing `MUDPROTO` / `name:`. Grayline account ≠ MUD character; Crossbar never sends `!name` / `!account` or a pad handle. Hangup: socket close, line `~.` or `QUIT`, or pad `bye`/`^C`. `/quit` is MudProto’s own command and is not disconnect.

See the MudProto project `README.md` → Greyline door section for the server side.

## Not on :8080

Do not add a link, a comment in the pad banner, or a `CONNECT admin` host. The public page is the terminal and nothing else.
