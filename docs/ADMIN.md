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

## Not on :8080

Do not add a link, a comment in the pad banner, or a `CONNECT admin` host. The public page is the terminal and nothing else.
