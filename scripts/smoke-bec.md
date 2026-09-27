# bec smoke

Walk, no root password. Greyline login lands on `GL>`. Then:

```
connect bec
```

Expect `CONNECT 1200`, then `BIG-EVIL CORPORATION` and `login:`. Not `crawler@orientation>`. `connect terminal-addiction` is `NO CARRIER`.

```
guest
whoami
cat /etc/motd
ls /usr/games
cat /usr/games/fortune
```

The last one should fail for guest. `su` to the account whose GECOS says default, using that default. Read the fortune again. `su` to root with what the fortune says. Prompt becomes `bec#`.

```
useradd <your greyline handle>
cat /etc/passwd
cu bec-mf
cat /usr/payroll/memo
~.
logout
```

`logout` from the bec shell returns to `GL>` at T1.

`stty` and `date` show the real year. `ls -l` dates stay in 1985–1993. `pico /tmp/foo` is the screen editor; `^O` writes, `^X` returns to the prompt. `help` is not a walkthrough. `python` is not found.
