#!/usr/bin/env python3
"""Point the cloud copy of the site at a gateway.

The player asks the gateway for the manifest. The address of the gateway is
one line in js/config.js and nowhere else, so this is the only knob.

    ./set_api.py https://tv.exemplo.com            # set and push
    ./set_api.py                                   # show the current value
    ./set_api.py --host tv.exemplo.com             # from a bare host name

The value is written without a trailing slash. An empty value means "same
origin", which is the case when the site and the gateway are served by the
same host - the normal case for the host in the cloud.
"""

import argparse
import os
import re
import subprocess
import sys

CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "site", "js", "config.js")
LINE = re.compile(r"^var CINE_API = '.*?';$", re.M)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default=None,
                        help="the base of the gateway, empty for the same origin")
    parser.add_argument("--host", default=None,
                        help="a bare host name, turned into https://HOST")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    with open(CONFIG, encoding="utf-8") as handle:
        body = handle.read()

    if args.url is None and args.host is None:
        match = LINE.search(body)
        print(match.group(0) if match else "var CINE_API = '';")
        return 0

    value = args.url if args.url is not None else ("https://" + args.host)
    value = value.strip().rstrip("/")
    if value and not value.startswith("http://") and not value.startswith("https://"):
        value = "https://" + value

    if not LINE.search(body):
        print("CINE_API not found in %s" % CONFIG, file=sys.stderr)
        return 1
    body = LINE.sub("var CINE_API = '%s';" % value, body, count=1)
    with open(CONFIG, "w", encoding="utf-8") as handle:
        handle.write(body)
    print("CINE_API = '%s'" % value)

    site = os.path.dirname(os.path.dirname(CONFIG))
    if args.no_push:
        return 0

    def git(*command):
        return subprocess.run(("git",) + command, cwd=site, check=False).returncode

    if git("status", "--porcelain") != 0:
        return 0
    git("add", "-A")
    git("commit", "-m", "chore: gateway em %s" % value)
    git("push", "origin", "main")
    print("publicado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
