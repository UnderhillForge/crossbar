# Crossbar

Crossbar 0.3.1 is the terminal switchboard for GRAYLINE. The public face is [grayline.dev](https://grayline.dev): one full-screen terminal and a private session per browser.

This repo is the app. It runs on the VPS behind the existing nginx. TLS stays on nginx. The in-world machine is GRAYLINE. The lobby has no year. A door's period starts after `CONNECT`.

## Local run

Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m crossbar
```

Open http://127.0.0.1:8080 . The process listens on `127.0.0.1:8080` only.

`CROSSBAR_SECRET` signs the session cookie. When it is unset, Crossbar uses a built-in local development value and logs a warning. On the VPS, put a long random secret in `/etc/crossbar.env`:

```
CROSSBAR_SECRET=replace-with-a-long-random-string
```

Two more variables are read by the server when a logged-in account uses `claim` or `chain`. The browser never opens this socket. Leave the key unset in development; Crossbar will say the awards key is not set and will not mark a claim.

```
PISECURE_RPC_URL=ws://127.0.0.1:3144
PISECURE_AWARDS_KEY=
```

`PISECURE_RPC_URL` defaults to `ws://127.0.0.1:3144`. `PISECURE_AWARDS_KEY` is a 64-character hex Ed25519 seed. It stays in the environment. Do not commit it, and do not store a player's spending key. A player account stores one `ps1` address.

## Logon

First paint:

```
CONNECTED  T1    DTE 03    NODE GL-01

GREYLINE PUBLIC DATA NETWORK
PAD READY

LOGON:
```

At `LOGON:`:

- a blank line or `guest` prints the reverse-video header, `GUEST ACCEPTED`, `CIRCUIT OPEN`, and `GL>`. Guest has no database row.
- a known handle asks `PASSWORD:`. A match prints the header, `IDENTITY <handle>`, `CIRCUIT OPEN`, and `GL>`. A bad password is `IDENTIFICATION NOT RECOGNIZED` and the prompt stays at `LOGON:`.
- `new` asks for a handle, a password, the password again, and an email. Passwords are at least 4 characters and are stored with stdlib scrypt. Nothing sends email. `verify` replies `verification is dark`. New accounts get status `ok`.

These handles cannot register or log in: `sysop`, `admin`, `root`, `operator`, `postmaster`, `grayline`, `crossbar`. `finger sysop` and `finger admin` say they are not accepting mail. `guest`, `new`, and `login` are prompt words, not accounts.

The operator console can close guest logon or registration. Closed guest is `logon closed`. Closed registration is `registration is closed`.

The lobby prompt is always `GL>`. Tab at `GL>` completes pad commands and host names. An unknown command prints `not found`.

## Circuits

`HOSTS` on the pad:

```
NAME     TITLE                              BAUD   STATE
BEC      ORIENTATION  •  BEC OUTSIDE PLANT      1200   UP
TA       TERMINAL ADDICTION                     2400   OFFLINE
TYMNET   CARRIER HOP                            T1     UP
```

There is no host named orientation. `CONNECT BEC` (alias `big-evil`) prints `DIALING 1200...` and enters the UNIX V7 door. The shell prompt there is `bec$` or `bec#`. `logout` or `bye` from that door returns to `GL>` at T1. `CONNECT TA` or `terminal-addiction` prints `DIALING 2400...` and `NO CARRIER` and leaves the session on the pad. `CONNECT TYMNET` is the carrier hop. `bye` or `g` there returns to the pad. `office-314` is on the Tymnet directory and is dark.

Greyline is T1 (1,544,000). A hop takes the lower of the current rate and the next host's ceiling, and the rate stays down until the path is Greyline alone. Output pacing happens on the WebSocket write. `MAP` and `HOSTS /T` draw the same three-gate tree. `FULL` is the pad, identity, and gates panes plus `NEWS UNAVAILABLE`. `NEWS` stays `NEWS UNAVAILABLE` until NNTP is wired. `DATE` is the real clock. `STATUS` is node, identity, destination, and baud. `FINGER` is identity, destination, and a grant count.

`?` lists the pad commands. The operator console can replace that text and the MOTD without a deploy. Host state (`UP`, `OFFLINE`, `MAINT`) is the same kind of override, in `data/site/runtime.json`.

## Profile and claims

These three commands exist only at `GL>`, and only for a real account. Guest gets `logon required`.

| Command | What it does |
| --- | --- |
| `profile_config` | Menu for this account. The prompt stays `GL>PROF_CON>` until `Q`. `1` changes email, `2` changes the password, `3` links one `ps1`. A blank line at a field returns to the menu. A short name is resolved with `namelookup` on `PISECURE_RPC_URL`, then on each node from the bootstrap directory, and the stored value is the `ps1`. The same `ps1` cannot sit on two accounts. If the node answers `listunspent`, the menu shows that balance. |
| `claim FLAG_ID` | Claims that flag for the linked `ps1`. With no link: `link a wallet first: ps1 link can be found in profile_config`. A claim already stored for this account, or a `ps1` that `listflags` already shows for that flag, replies `already yours` and does not submit again. Otherwise the server submits `claimflag`, signed by the awards key, with `flag_id` and that `ps1`. The doorway answer stays in Crossbar. Acceptance replies `claimed` and stores the flag id, `ps1`, and txid on the account. The same claim works from the BEC shell when the id is not a local BEC stamp. |
| `pschain` | Difficulty, height, tip hash, network hashrate, health, and nodes. Uses the pisecured node when it answers, otherwise `https://pisecure-bootstrap-production.up.railway.app` (`/api/v1/network/live` and `/api/v1/nodes/list`). A hashrate the directory does not report is `unavailable`. |
| `leaders` | Top 20 accounts by accepted claims on this hub. Ties share a rank. |
| `user_list` | Every registered account, its leaderboard rank when it has claims, and last login. |

Node replies that Crossbar prints as one line: `unknown flag`, `already claimed`, `flag exhausted`, `flag expired`, `unlimited flag cannot pay`. A missing flag method is `flags are not on this node yet`, and the account is not marked claimed. A missing `getchaininfo` is `getchaininfo is not on this node yet`. `createflag` is not a lobby command.

## Refresh

`GET /` stores a session id in a signed HttpOnly cookie named `crossbar`. The cookie is valid on local HTTP, so `http://127.0.0.1:8080` works without TLS. The page opens a WebSocket to `/ws`, and the server looks up that id.

Reload joins the same `Session` when the process still has it and it has been idle for less than 6 hours. A logged-in pad reprints the reverse-video header and `GL>`. A session that is still inside a hop prints `resumed name@host` and that hop's prompt.

`logout` and `exit` clear the name and print `LOGON:` again. The cookie and the session id stay.

Browser Back stays on `/`. Backspace deletes the last character of the line being typed and the server echoes `\b \b`. Password entry, including the password lines inside `profile_config`, is not echoed.

## Persistence

A `Session` holds the sid, user, host, line buffer, command history, and last activity, in process memory. A restart clears sessions. The next visit still sends the cookie, finds no session, and shows the logon paint again. Idle sessions are dropped after 6 hours.

Accounts are rows in `data/grayline.db`: handle, password hash, email, created, last login, status, must-change, note, and wallet. Accepted flag claims are rows in `flag_claims` (handle, flag id, `ps1`, txid, time). `CROSSBAR_SECRET` still only signs the cookie. Set `CROSSBAR_DB` to point at a different file.

To wipe users, stop Crossbar and delete `data/grayline.db`. The next start creates an empty database. Guest sessions are not in that file. `data/*.db`, `data/users/`, `data/site/`, and `data/admin-audit.log` are gitignored.

Registered play on the V7 door is an overlay under `data/users/<handle>/bec/`. Guest edits live on the session and are dropped on hangup. The gold tree under `packs/big-evil/tree/` is not written at runtime.

## Operator console

The public pad does not link to the console. Bind and first-time setup are in [docs/ADMIN.md](docs/ADMIN.md).

```bash
python -m crossbar --admin
```

That process listens on `127.0.0.1:8081`. Do not publish that port. Operator accounts are separate from Greyline handles and live in `data/admin.db`.

## nginx

The app listens on localhost. nginx on the VPS is the TLS endpoint and the reverse proxy. Example for a later hand edit of that host:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

server {
    server_name grayline.dev;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 21600s;
    }
}
```

WebSocket upgrade headers are required. `proxy_read_timeout` is 6 hours so a quiet terminal survives as long as the session idle window. TLS stays on nginx. This repo leaves the live host alone.

## systemd

[`deploy/crossbar.service`](deploy/crossbar.service) is an example unit: `User=crossbar`, `WorkingDirectory=/opt/crossbar`, `ExecStart` runs uvicorn on `127.0.0.1:8080`, `Restart=always`. Installing it on the VPS is a later step. Adjust the paths to match where the checkout and the virtualenv actually live.

## Tests

```bash
python -m unittest tests.test_app tests.test_admin tests.test_claim
```

`tests.test_claim` talks to a node only when `PISECURE_RPC_URL` accepts a connection. Otherwise it checks the lobby parser: missing link, already yours, and a refused call. It does not invent an accepted claim.
