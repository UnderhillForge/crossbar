# UNIX flavors

The shell in `crossbar/v7.py` reads pack data. It does not hardcode a host layout.

```
packs/<name>/catalog.yaml   # exec, dev, catalog-only files, uname, df, PATH
packs/<name>/tree/          # text clutter. Runtime never writes here.
packs/<name>/manifest.yaml  # gold inodes for those text files
packs/<name>/passwd         # optional. If missing, the shell reads tree/etc/passwd.
```

`catalog.yaml` keys the interpreter understands: `uname`, `uname_mf`, `path`, `unix`, `dirs`, `bin` (`/bin`), `usrbin` (`/usr/bin`), `localbin` (`/usr/local/bin`), `devs`, `lib`, `df`. `type=exec` and `type=dev` have no byte file. A catalog path wins over a text file at the same path, so `/bin/ls` stays a binary.

Player edits go to `data/users/<handle>/bec/state.json` when the Greyline handle is a registered account. Guest edits stay on the session and are dropped at logout.

`era` on the pack and in `catalog.yaml` is that door's period (`1985-1993` for bec). The Greyline PAD has no era: its banner, help, hosts, and news use the real clock and do not borrow a door's dates. A live news feed, if one is added, stays on the PAD.

To add `flavor: linux31`, create `packs/linux31/` with its own catalog (GNU names, `/home`, `/var`, its own `era`, `uname`, and `df`) and tree. Set the pack's `flavor`, `data`, and `era` fields. Do not add the layout to the interpreter.
