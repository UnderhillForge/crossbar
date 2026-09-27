# Crossbar

Crossbar is the terminal switchboard for GRAYLINE. The public face is [grayline.dev](https://grayline.dev): one full-screen terminal, a login prompt, and a private session per browser.

This repo is the app. It runs on the VPS behind the existing nginx. TLS stays on nginx.

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

## Login

The terminal plays a short carrier sequence, then an ASCII banner, then `login:`. Any key after `CONNECT` skips the rest of the boot. Left alone, the sequence finishes in under 3 seconds.

At `login:`:

- `guest` is ephemeral. It has no row in the database.
- a handle asks for that account's password
- `new` asks for a handle, a password, the password again, and an email

The glass opens at `LOGON:`. Guest or a blank line is `GUEST ACCEPTED`. A known account is `IDENTITY <handle>`. The lobby prompt is `GL>`. Passwords are stored with stdlib scrypt. Nothing sends email. `verify` replies `verification is dark`. New accounts get status `ok`.

These handles cannot register or log in: `sysop`, `admin`, `root`, `operator`, `postmaster`, `grayline`, `crossbar`. `finger sysop` and `finger admin` say they are not accepting mail. Those two names have no working password. `guest`, `new`, and `login` are prompt words, not accounts.

After logon the prompt is `GL>`. `CONNECT BEC` leaves the pad for the V7 door. `CONNECT TA` is `NO CARRIER`.

## Refresh

`GET /` stores a session id in a signed HttpOnly cookie named `crossbar`. The cookie is valid on local HTTP, so `http://127.0.0.1:8080` works without TLS. The page opens a WebSocket to `/ws`, and the server looks up that id.

Reload joins the same `Session` when the process still has it and it has been idle for less than 6 hours. A logged-in terminal prints:

```
resumed name@host
name@host>
```

`logout` and `exit` clear the name and print the login banner again. The cookie and the session id stay.

Browser Back stays on `/`. Backspace deletes the last character of the line being typed and the server echoes `\b \b`.

## Persistence

A `Session` holds the sid, user, host, line buffer, command history, and last activity, in process memory. A restart clears sessions. The next visit still sends the cookie, finds no session, and shows the boot sequence again. Idle sessions are dropped after 6 hours.

Accounts are rows in `data/grayline.db`: handle, password hash, email, created, last login, and status. `CROSSBAR_SECRET` still only signs the cookie. Set `CROSSBAR_DB` to point at a different file.

To wipe users, stop Crossbar and delete `data/grayline.db`. The next start creates an empty database. Guest sessions are not in that file.

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

## Commands

| Command | What it does |
| --- | --- |
| `help`, `?` | the short list |
| `ls`, `dir` | lobby entries: `motd`, `hosts`, `mail` |
| `who` | names of other live sessions |
| `motd` | message of the day |
| `finger [name]`, `whois` | a live session (you, when the name is omitted) |
| `hosts`, `host` | grayline's directory: grayline, tymnet, terminal-addiction |
| `mail` | `no letters.` |
| `verify` | `verification is dark` |
| `connect <host>` | joins an in-process pack and changes the prompt |
| `login` | you are already in; `logout` leaves |
| `logout`, `exit` | back to the login banner, same sid |

`connect` does not open a TCP connection. Grayline is the default pack. `connect tymnet` puts you on the Tymnet pad (`name@tymnet>`). Its `hosts` list is grayline, terminal-addiction, and office-314. office-314 is dark. `connect terminal-addiction` from grayline or tymnet opens a short Renegade menu with no messages yet. `bye` or `g` on that board returns to the previous host, or to grayline. On tymnet, `bye` and `g` return to grayline.

From Terminal Addiction, `d` opens Doors and `1` dials `bec`, a simulated UNIX V7 box at 1200 baud. The prompt is a shell (`bec$` or `bec#`), not the orientation booth. Baud only drops as you leave Greyline and returns to T1 when the path is home again. `packs/orientation/` is still in the tree and is not door 1. The annex under `packs/orientation-target/` is unchanged and is not started for this hop. An unknown command prints `not found`.

## Tests

```bash
python -m unittest tests/test_app.py
```
