#!/usr/bin/env python3
"""Make the cloud copy of the resolver.

The resolver on the box is written for the box: its paths point at /home/
samba/plex, its base URL is taken from the Host header of the request, and
it says nothing about CORS. On a host in the cloud three things have to be
different, and this script makes exactly those three changes on a copy. The
resolver on the box is never touched.

  1. Paths come from the environment, so the cloud host keeps its own
     layout (RESOLVER_HOME, RESOLVER_SITE_DIR, RESOLVER_CATALOG_DIR,
     RESOLVER_POSTER_DIR, RESOLVER_M3U_PATH, RESOLVER_FFMPEG).

  2. The public base URL can be pinned (RESOLVER_PUBLIC_BASE). Behind a
     reverse proxy the Host header carries the domain without a port and
     without the scheme, and the manifest would then point the television
     at http://domain:8730 - a port that is not open.

  3. CORS is optional (RESOLVER_CORS_ORIGIN). The site on the GitHub Pages
     talks to the gateway on another host, and the browser needs the answer
     to carry Access-Control-Allow-Origin. Empty means no header at all,
     which is the same behaviour as the box.

Usage:
    patch_resolver.py [--source /home/samba/plex/bin/resolver.py]
                      [--out resolver.py]
"""

import argparse
import os
import sys

PATH_BLOCK = '''HOME = os.environ.get("RESOLVER_HOME", "/home/samba/plex")
SITE_DIR = os.environ.get("RESOLVER_SITE_DIR", os.path.join(HOME, "site"))
CATALOG_DIR = os.environ.get("RESOLVER_CATALOG_DIR", os.path.join(HOME, "cache"))
M3U_PATH = os.environ.get("RESOLVER_M3U_PATH", os.path.join(HOME, "cinestream.m3u"))
CORS_ORIGIN = os.environ.get("RESOLVER_CORS_ORIGIN", "").strip()
'''

REPLACEMENTS = [
    # 1. The paths.
    (
        'FFMPEG = os.environ.get("RESOLVER_FFMPEG", "/home/samba/plex/vendor/ffmpeg")',
        'FFMPEG = os.environ.get("RESOLVER_FFMPEG", os.path.join(HOME, "vendor", "ffmpeg"))',
    ),
    (
        'POSTER_DIR = "/home/samba/plex/cache/posters"',
        'POSTER_DIR = os.environ.get("RESOLVER_POSTER_DIR",\n'
        '                           os.path.join(CACHE_DIR, "posters"))',
    ),
    (
        'with open("/home/samba/plex/cinestream.m3u", "rb") as f:',
        'with open(M3U_PATH, "rb") as f:',
    ),
    (
        '        root = "/home/samba/plex/site"',
        '        root = SITE_DIR',
    ),
    (
        '        path = "/home/samba/plex/vendor/js/hls.min.js"',
        '        path = os.path.join(SITE_DIR, "js", "hls.min.js")',
    ),
    (
        '        path = "/home/samba/plex/cache/%s.json" % category',
        '        path = os.path.join(CATALOG_DIR, "%s.json" % category)',
    ),
    # 2. The pinned public base.
    (
        '    if host:\n'
        '        value = host.split(",")[0].strip()',
        '    if RESOLVER_PUBLIC_BASE:\n'
        '        return RESOLVER_PUBLIC_BASE\n'
        '    if host:\n'
        '        value = host.split(",")[0].strip()',
    ),
    # 2b. A Codespaces host: the port is forwarded under a name like
    # <codespace>-8730.app.github.dev and the answer is https, without a
    # port. The old rule would build http://<name>-8730.app.github.dev:8730.
    (
        '        if value and value not in ("localhost", "::1") and not value.startswith("127."):\n'
        '            return "http://%s:%d" % (value, PORT)',
        '        if value and value not in ("localhost", "::1") and not value.startswith("127."):\n'
        '            if value.endswith(".app.github.dev"):\n'
        '                return "https://%s" % value\n'
        '            return "http://%s:%d" % (value, PORT)',
    ),
    # 3. CORS.
    (
        '        self.send_response(code)\n'
        '        for key, value in headers.items():',
        '        if CORS_ORIGIN:\n'
        '            headers.setdefault("Access-Control-Allow-Origin", CORS_ORIGIN)\n'
        '            headers.setdefault("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")\n'
        '            headers.setdefault("Access-Control-Allow-Headers", "*")\n'
        '            headers.setdefault("Access-Control-Max-Age", "600")\n'
        '        self.send_response(code)\n'
        '        for key, value in headers.items():',
    ),
]

OPTIONS_HANDLER = '''
    def do_OPTIONS(self):
        """Answer the preflight of a cross-origin request.

        Only sent when RESOLVER_CORS_ORIGIN is set. Without it the browser
        never asks, because the site and the gateway are on one host.
        """
        if not CORS_ORIGIN:
            self._send(405, b"", {"Content-Type": "text/plain"})
            return
        self._send(204, b"", {"Content-Type": "text/plain"})

'''

ANCHOR = "    def do_HEAD(self):"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/home/samba/plex/bin/resolver.py")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "resolver.py"))
    args = parser.parse_args()

    with open(args.source, encoding="utf-8") as handle:
        body = handle.read()

    # The path block goes in right after the port and mode.
    anchor = 'MODE = os.environ.get("RESOLVER_MODE", "proxy")\n'
    if anchor not in body:
        print("anchor for the path block not found", file=sys.stderr)
        return 1
    body = body.replace(anchor, anchor + PATH_BLOCK, 1)

    for old, new in REPLACEMENTS:
        if old not in body:
            print("not found: %s" % old.splitlines()[0][:70], file=sys.stderr)
            return 1
        body = body.replace(old, new, 1)

    if ANCHOR not in body:
        print("anchor for do_OPTIONS not found", file=sys.stderr)
        return 1
    body = body.replace(ANCHOR, OPTIONS_HANDLER.lstrip("\n") + ANCHOR, 1)

    # The cache directory is used by the poster path block, which sits above
    # the place the path block was inserted. Give it its own definition.
    body = body.replace(
        'CACHE_DIR = os.environ.get("RESOLVER_CACHE_DIR", os.path.join(HOME, "cache"))\n', '')
    body = body.replace(
        'M3U_PATH = os.environ.get("RESOLVER_M3U_PATH", os.path.join(HOME, "cinestream.m3u"))\n',
        'M3U_PATH = os.environ.get("RESOLVER_M3U_PATH", os.path.join(HOME, "cinestream.m3u"))\n'
        'CACHE_DIR = os.environ.get("RESOLVER_CACHE_DIR", os.path.join(HOME, "cache"))\n',
        1)

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(body)

    os.chmod(args.out, 0o755)
    print("cloud resolver written: %s (%d lines)" % (args.out, body.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
