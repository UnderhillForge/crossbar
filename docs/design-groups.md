# Grayline GROUPS (Local Forums + Packet / NNTP Roadmap)

| Field | Value |
| --- | --- |
| **Author** | jws |
| **Date** | 2026-10-01 |
| **Status** | Ready for implementation |
| **Workspace** | `/Volumes/External Drive/coding/crossbar` |

---

## Overview

Grayline needs Usenet-style discussion forums on the pad without overloading **NEWS** (the operator system bulletin in `data/text/news.asc`).

**GROUPS** is the pad verb for forums. v1 is fully local: list groups, select a group, headers, read, next/prev, and post. Later layers add signed **packet bundles** between Crossbar sites (Fido/UUCP feel) and an optional **NNTP gateway** to public or private Usenet.

BEC door `news` stays pack-local and must never collide with pad GROUPS.

---

## Background

| Piece | Today |
| --- | --- |
| `NEWS` | Prints `news.asc` (or operator override) — system chrome |
| `WALL` | Short graffiti file `wall.asc` |
| `MAIL` | Pad-local letters between handles |
| Admin | `news_allow` in runtime.json; NNTP note still “not configured” |
| BEC `news` | Reads pack tree files only |

Players need threaded, multi-author, multi-group discussion. Operators need a path to mesh sites and, eventually, real Usenet — without turning every Crossbar into an open NNTP relay on day one.

---

## Goals

- Local forums with classic reader UX (`GROUPS` / `HEADERS` / `READ` / `POST` / `NEXT`).
- Stable `Message-ID` and `References` from day one (dedupe for sync).
- Keep **NEWS** as system bulletin forever.
- Roadmap: packet mesh (Crossbar↔Crossbar), then optional NNTP up/downlink.
- Guest may read; registered handles post (policy per group).

## Non-goals (v1)

- Packet sync, NNTP, cross-site
- Killfiles, scores, MIME, binaries
- Soft-delete / cancel articles
- Replacing `news.asc` or BEC `news`

---

## Naming

| Surface | Role |
| --- | --- |
| `NEWS` | System bulletin (`news.asc`) |
| `GROUPS` | Forum reader/poster |
| Group names | `grayline.general`, `grayline.doors`, `grayline.sysop` |
| Mesh | **packet** / **bundle** |
| Public Usenet | **NNTP gateway** (phase 3) |

---

## Architecture

```text
Pad GROUPS  →  SQLite articles/groups  →  packet mesh (later)
                                   └→  NNTP gateway (later)
```

Local store is source of truth. Sync layers import/export; they do not bypass pad auth.

---

## v1 Pad UX

```text
GROUPS                     list groups (+ unread when logged in)
GROUPS LIST
GROUPS <name>              select current group
GROUPS GROUP <name>        same
GROUPS HEADERS [n]         recent headers (default 20)
GROUPS READ <n>            article by per-group number
GROUPS NEXT / PREV
GROUPS POST [subject]      compose (. ends, Q/^C cancel)
GROUPS HELP
```

- Grayline only; other hosts → `groups: not found`.
- Compose phases: `group_subject` / `group_body` (mirror MAIL).
- Prompt `[path]=groups` during compose only.
- Ctrl-C / lone `Q` cancel; Tab suppressed during compose.

### Seed groups

| Group | `post_policy` | Notes |
| --- | --- | --- |
| `grayline.general` | `members` | default |
| `grayline.doors` | `members` | circuits / doors |
| `grayline.sysop` | `sysop` | read for all; pad POST rejected; console/API as `sysop` later |

### Post rules

- `members`: registered account `status=ok`, not guest.
- `sysop`: reject on pad with `read only` / `sysop only`.
- `readonly`: reject all posts.
- Empty body rejected; empty subject allowed → `(no subject)`.
- Caps: subject ≤60, body ≤60 lines / 4000 chars (same spirit as mail).

---

## Data model

```sql
groups (
  name TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  post_policy TEXT NOT NULL DEFAULT 'members',  -- members|sysop|readonly
  created TEXT NOT NULL
);

articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT UNIQUE NOT NULL,
  group_name TEXT NOT NULL,
  number INTEGER NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  from_handle TEXT NOT NULL,
  date_sent TEXT NOT NULL,
  date_arrived TEXT NOT NULL,
  references_hdr TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'local',  -- local|packet|nntp
  UNIQUE(group_name, number)
);

group_read (
  handle TEXT NOT NULL,
  group_name TEXT NOT NULL,
  last_read INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (handle, group_name)
);
```

**Message-ID:** `<{id}.{utc_compact}@{site_tag}>` after insert (or pre-allocated).  
**site_tag:** `CROSSBAR_SITE_ID` if set, else `SYSTEM_NAME` (`grayline`).  
**Path:** empty on local post; phase 2 appends `!site_tag`.

### API sketch (`crossbar/groups.py`)

```python
def ensure_schema(conn) -> None: ...
def seed_default_groups() -> None: ...
def list_groups() -> list[GroupInfo]: ...
def get_group(name: str) -> GroupInfo | None: ...
def headers(group: str, *, limit: int = 20) -> list[ArticleHeader]: ...
def get_article(group: str, number: int) -> Article | None: ...
def next_article(group: str, number: int) -> Article | None: ...
def prev_article(group: str, number: int) -> Article | None: ...
def post(*, group: str, from_handle: str, subject: str, body: str,
         references: str = "") -> int: ...  # returns article number
def mark_read(handle: str, group: str, number: int) -> None: ...
def unread_count(handle: str, group: str) -> int: ...
```

---

## Phase 2 — Packet mesh

Signed store-and-forward **bundles** between Crossbar sites.

- Format: JSON manifest + articles (`.cbrpkt` or HTTPS body)
- `site_id`, `bundle_id`, `groups[]`, `articles[]`, ed25519 `signature`
- Peers allowlist: `data/site/peers.json`
- Transport: HTTPS push to `/_crossbar/packet` and/or poll drop-box
- Loop control: `Path` + Message-ID dedupe
- Per-group **echo** flag: which groups leave the site
- Admin `/o/groups`: CRUD, peers, last in/out, force push

Fiction: circuit mailbags between PDN nodes — do not print “FidoNet” on the pad.

## Phase 3 — NNTP gateway

- Optional up/down to upstream NNTP for allowlisted groups
- Off by default; `news_allow` may become the NNTP allowlist
- From: `handle@site_tag`; no spoofed real-world emails without opt-in
- Same article table; `source='nntp'`

---

## Security

| Risk | Mitigation |
| --- | --- |
| Guest spam | post requires registered handle |
| Cross-site forgery | signed bundles + peer allowlist (phase 2) |
| Usenet blowback | uplink off by default (phase 3) |
| Path loops | Path + Message-ID |
| Huge posts | subject/body caps |

---

## Collision map

| Path | Behavior |
| --- | --- |
| Pad `NEWS` | `news.asc` only |
| Pad `GROUPS` | forums |
| BEC `news` | pack files |
| Admin mail page | broadcasts; NNTP note remains until phase 3 |

---

## Test plan (v1)

| Case | Expect |
| --- | --- |
| Seed groups exist after migrate | three names |
| Post as ada to general | number 1; Message-ID set |
| Guest POST | logon required |
| POST to sysop group | rejected |
| HEADERS / READ / NEXT / PREV | order correct |
| mark_read / unread | LIST shows unread |
| `host=bec` then groups | not found / door unaffected |
| Compose Q / ^C | no partial article |
| Duplicate Message-ID import (unit) | ignored (phase 2 hook optional in v1 tests) |

---

## PR Plan

### PR 0 — Design doc
- **Title:** `docs: design GROUPS (local forums + packet/NNTP roadmap)`
- **Files:** `docs/design-groups.md`

### PR 1 — Schema + API
- **Title:** `groups: local store and API`
- **Files:** `crossbar/groups.py`, `accounts._migrate`, `tests/test_groups.py`
- **Description:** Schema, seed, post/headers/get/next/prev/unread; no pad verb.

### PR 2 — Pad verb
- **Title:** `groups: pad reader and poster`
- **Files:** `commands.py`, `session.py`, `lobby.py`, `packs.py`, `ws.py`, `main_menu.asc`, tests
- **Description:** Full GROUPS UX + compose; grayline only.

### PR 3 — Unread polish + docs
- **Title:** `groups: unread counts and README/news`
- **Files:** LIST unread wiring, `README.md`, `news.asc`, `ADMIN.md`
- **Dependencies:** PR 2

### PR 4+ — Packet / admin / NNTP
- Deferred; see phases 2–3 above.
