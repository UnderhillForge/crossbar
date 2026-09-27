# Orientation annex

Private game prop for a Grayline floor. It is a static kiosk: a linked pamphlet and one unlinked note. Attacking grayline.dev itself is out of scope and boring. This stack is not the hub, and it is not a place to practice against the hub.

Crossbar plays Floor 0 from `packs/orientation/copy` with no container running. The annex is only the paper the stairwell talks about.

## Run

From this directory:

```bash
docker compose up -d --build
```

`compose.yaml` publishes no host ports. The service sits on an internal bridge. Other containers on that network can open `http://orientation-target:8080/`. The Grayline host cannot, until someone deliberately adds a loopback publish. A future door proxy, not built in this slice, would be a stub in Crossbar (`door_proxy()` returns nothing) and a bind such as `127.0.0.1:8765:8080` added on purpose. Do not attach this service to the Grayline nginx site. Do not listen on `0.0.0.0:80` on the VPS.

`FLAG_OPEN=0` serves the "closed for remodeling" page for every path except `/healthz`. Set it in the compose `environment` block.

## Reset

```bash
docker compose down -v && docker compose up -d --build
```

There is no volume worth keeping. The sheets are the image.

## What is in the annex

- `www/kiosk.html` links `www/pamphlet.txt`. That sheet is the first prize code.
- `www/notes/night-shift.txt` is not linked from the index. The vending receipt and the Floor 0 news name it. That sheet is the second prize code.
- The committed codes are placeholders. They match `packs/orientation/flags.env.example`. For a live event, change the sheets and a local `packs/orientation/flags.env` together. Do not commit live values. `flags.env` is gitignored. Nothing in this image is served by nginx on grayline.dev.

## What Crossbar must not mount

Do not give this container:

- the Crossbar repo
- `/`
- `/var/lib/pisecure`
- nginx keys, origin certs, or the Grayline site config
- `docker.sock`

No shells, no upload form, no database, no extra process. The process serves files from `/lab/www` and answers `/healthz`.
