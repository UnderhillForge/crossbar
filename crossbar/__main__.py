"""python -m crossbar — public pad on 127.0.0.1:8080. --admin is a separate port."""

import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--admin"]:
        from crossbar.adminapp import serve_admin

        serve_admin()
        return
    if args[:2] == ["admin", "bootstrap"]:
        from crossbar.operators import issue_setup_token

        token = issue_setup_token()
        if not token:
            sys.stderr.write("operators already exist\n")
            raise SystemExit(1)
        sys.stdout.write(token + "\n")
        return
    from crossbar.app import main as serve_public

    serve_public()


if __name__ == "__main__":
    main()
