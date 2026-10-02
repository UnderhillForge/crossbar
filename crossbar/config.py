"""Runtime settings. The process listens on 127.0.0.1:8080 only.

MudProto door (CONNECT MUDPROTO) reads MUDPROTO_HOST / MUDPROTO_PORT
(defaults 127.0.0.1:4000). That port must stay loopback — TLS stays on
Grayline. See crossbar/mudproto.py and docs/ADMIN.md.
"""

from __future__ import annotations

import os

from crossbar import __version__

VERSION = __version__
HOST = "127.0.0.1"
PORT = 8080
# In-world pad name for [host] in data/text/prompt.asc.
SYSTEM_NAME = "grayline"
IDLE_TTL = 6 * 60 * 60
COOKIE_NAME = "crossbar"
# The cookie outlives one page view so a refresh can still present the sid.
# The server, not the cookie, drops a Session after IDLE_TTL of silence.
COOKIE_MAX_AGE = 14 * 24 * 60 * 60
MAX_LINE = 240
MAX_HISTORY = 200
# Greyline lobby speed. Other hops only clamp downward until the path is home.
T1_BAUD = 1_544_000
_DEV_SECRET = "dev-only-crossbar-secret"


def load_secret() -> tuple[str, bool]:
    secret = os.environ.get("CROSSBAR_SECRET", "").strip()
    if secret:
        return secret, False
    return _DEV_SECRET, True


SECRET, USING_DEV_SECRET = load_secret()
