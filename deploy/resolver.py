#!/usr/bin/env python3
# Python 3.8, Linux. Local HTTP resolver for the CineStream Plex library.
#
# Plex reads .strm files that point at this service. Each request resolves the
# title against the WatchPlay player page and hands Plex a freshly signed HLS
# playlist URL. Signed links expire after a few minutes, so nothing is baked
# into the .strm files: resolution happens at play time and is cached briefly.
#
# Routes:
#   GET /stream/movie/<tmdb_or_imdb>          -> 302 to signed playlist
#   GET /stream/tv/<tmdb>/<season>/<episode>  -> 302 to signed playlist
#   GET /stream/imdb/<tt id>                   -> 302 to signed playlist
#   GET /tv/movie/<id>                        -> MPEG-TS playlist for the TV
#   GET /tv/tv/<id>/<season>/<episode>        -> MPEG-TS playlist for the TV
#   GET /check/movie/<id>                     -> JSON: ok | missing | transient
#   GET /healthz                               -> JSON status
#
# Env:
#   RESOLVER_HOST (default 127.0.0.1) RESOLVER_PORT (default 8730)
#   RESOLVER_MODE=redirect|proxy  RESOLVER_CACHE_TTL (default 300 seconds)
#   RESOLVER_FFMPEG (default /home/samba/plex/vendor/ffmpeg)

import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("RESOLVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("RESOLVER_PORT", "8730"))
MODE = os.environ.get("RESOLVER_MODE", "proxy")
HOME = os.environ.get("RESOLVER_HOME", "/home/samba/plex")
SITE_DIR = os.environ.get("RESOLVER_SITE_DIR", os.path.join(HOME, "site"))
CATALOG_DIR = os.environ.get("RESOLVER_CATALOG_DIR", os.path.join(HOME, "cache"))
M3U_PATH = os.environ.get("RESOLVER_M3U_PATH", os.path.join(HOME, "cinestream.m3u"))
CACHE_DIR = os.environ.get("RESOLVER_CACHE_DIR", os.path.join(HOME, "cache"))
CORS_ORIGIN = os.environ.get("RESOLVER_CORS_ORIGIN", "").strip()
CACHE_TTL = int(os.environ.get("RESOLVER_CACHE_TTL", "300"))
WATCHPLAY = "https://v2.watchplay.shop"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"

# ffmpeg is used to turn the CDN's CMAF/fMP4 fragments into MPEG-TS. The
# players on the TV (SS IPTV and the browser's own hls.js) do not implement
# EXT-X-MAP, so an fMP4 playlist never yields a frame: the player skips the
# init segment and retries the same fragment until it gives up. MPEG-TS is
# the container every one of them understands.
FFMPEG = os.environ.get("RESOLVER_FFMPEG", os.path.join(HOME, "vendor", "ffmpeg"))
REMUX_TIMEOUT = int(os.environ.get("RESOLVER_REMUX_TIMEOUT", "30"))

_cache = {}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "errors": 0, "proxied": 0}

# One resolve per title at a time. The prefetcher and the player ask for the
# same title at the same moment, and the upstream host is not fast.
_resolve_locks = {}


def _resolve_lock(key):
    with _lock:
        found = _resolve_locks.get(key)
        if found is None:
            found = threading.Lock()
            _resolve_locks[key] = found
        return found

# The upstream playlist is fetched again for every request, and every request
# for the same title arrives with the same signed URL. It is kept for a short
# time: the segment signatures inside it are the ones of the playlist, so a
# stale copy would hand out expired segments.
_playlist_cache = {}
_PLAYLIST_TTL = 120
_PLAYLIST_MAX = 64

_URL_RE = re.compile(r'url:"([^"]+)"')
_TYPE_RE = re.compile(r'type:"([^"]+)"')
_MASTER_RE = re.compile(r'^#EXT-X-STREAM-INF:.*$', re.M)


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    sys.stderr.write(line)
    sys.stderr.flush()


def http_get(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def resolve_page(url):
    """Return (playlist_url, media_type) from a WatchPlay player page."""
    body = http_get(url).decode("utf-8", "replace")
    match = _URL_RE.search(body)
    if not match:
        return None, None
    playlist = match.group(1).replace("\\/", "/")
    type_match = _TYPE_RE.search(body)
    media_type = type_match.group(1) if type_match else ""
    if media_type and media_type != "m3u8":
        return None, None
    return playlist, media_type


def expiry_of(playlist):
    match = re.search(r"expires=(\d+)", playlist)
    if not match:
        return time.time() + CACHE_TTL
    return int(match.group(1))


def cached_get(key, resolver):
    """Resolve with a short cache and single-flight behaviour."""
    now = time.time()
    with _lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            _stats["hits"] += 1
            return entry[1], entry[2]

    # Two callers that miss the cache at the same moment resolve once. The
    # second one waits here and reads the answer out of the cache instead of
    # asking the upstream host a second time.
    with _resolve_lock(key):
        now = time.time()
        with _lock:
            entry = _cache.get(key)
            if entry and entry[0] > now:
                _stats["hits"] += 1
                return entry[1], entry[2]

        playlist, media_type = resolver()

        with _lock:
            if playlist:
                ttl = CACHE_TTL
                expires = expiry_of(playlist)
                remaining = expires - time.time()
                if remaining > 0:
                    ttl = min(CACHE_TTL, max(30, int(remaining - 20)))
                _cache[key] = (time.time() + ttl, playlist, media_type)
                _stats["misses"] += 1
            else:
                _cache[key] = (time.time() + 20, None, None)
                _stats["errors"] += 1
    return playlist, media_type


RESOLVER_PUBLIC_BASE = os.environ.get("RESOLVER_PUBLIC_BASE", "").strip().rstrip("/")


def lan_ip():
    """Return this machine's LAN address, or None when it cannot be read.

    `ip` lives in /sbin on Debian, which is not on the PATH of every service
    environment, so absolute paths are tried first.
    """
    for ip_bin in ("/usr/sbin/ip", "/sbin/ip", "ip"):
        try:
            out = subprocess.check_output(
                [ip_bin, "-4", "addr", "show", "scope", "global"],
                timeout=5, stderr=subprocess.DEVNULL).decode("utf-8")
        except Exception:
            continue
        for line in out.splitlines():
            if "inet " in line:
                address = line.split("inet ")[1].split("/")[0].strip()
                if address and not address.startswith("127."):
                    return address
    return None


def default_public_base():
    """Fallback base for callers that do not send a usable Host header."""
    if RESOLVER_PUBLIC_BASE:
        return RESOLVER_PUBLIC_BASE.rstrip("/")
    address = lan_ip()
    if address:
        return "http://%s:%d" % (address, PORT)
    return "http://127.0.0.1:%d" % PORT


def public_base_for(host):
    """Build the base URL the client must use to reach this service.

    The segment URLs inside the rewritten playlist have to be absolute and
    reachable from wherever the client is. The Host header the client used is
    the best answer to that: Plex asks for 192.168.1.106:8730 and the TV asks
    for the same, so the segments come back pointing at the LAN address. A
    loopback host falls back to this machine's LAN address, because
    http://127.0.0.1 on the client's own machine is dead on arrival.
    """
    if RESOLVER_PUBLIC_BASE:
        return RESOLVER_PUBLIC_BASE
    if host:
        value = host.split(",")[0].strip()
        if value.startswith("["):          # [::1]:8730
            value = value[1:].split("]")[0]
        else:
            value = value.split(":")[0] if value.count(":") == 1 else value
        if value and value not in ("localhost", "::1") and not value.startswith("127."):
            return "http://%s:%d" % (value, PORT)
    return default_public_base()


def rewrite_manifest(playlist, public_base):
    """Rewrite the signed CDN playlist into one this box can serve.

    Three things have to change.

    * The CDN names its segments .js (fragmented MP4) or .css (plain MPEG-TS).
      Plex' own transcoder refuses a segment whose name does not match the
      container it finds inside - it reads the segment, finds MPEG-TS where the
      name promised MP4 and drops the whole playlist with "detected format
      mpegts extension none mismatches allowed extensions". Every segment is
      therefore aliased with the extension of the container it really holds:
      .mp4 for a playlist with an EXT-X-MAP init segment, .ts for a plain
      MPEG-TS one.
    * The URLs have to be absolute and reachable from the TV, so they carry
      the LAN address of this machine instead of a relative path.
    * The signed query stays on the playlist. The segments are addressed by
      their own path: on the anime tree the CDN checks the signature against
      the file it was issued for and answers 404 when it finds the playlist's
      signature on a segment.
    """
    body = playlist_body(playlist)
    base = playlist.rsplit("/", 1)[0] + "/"

    extension = "mp4" if "#EXT-X-MAP" in body else "ts"

    def cdn_url(value):
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("/"):
            origin = playlist.split("/", 3)
            root = "/".join(origin[:3])
            return root + value
        return base + value

    counter = {"n": 0}

    def segment_url(cdn):
        counter["n"] += 1
        return "%s/seg/%06d.%s?u=%s" % (public_base, counter["n"], extension,
                                       urllib.parse.quote(cdn, safe=""))

    def line_url(match):
        line = match.group(0).strip()
        if not line or line.startswith("#"):
            return match.group(0)
        return segment_url(cdn_url(line))

    def attr_url(match):
        value = match.group(1)
        if value.startswith("http://") or value.startswith("https://"):
            return 'URI="%s"' % value
        return 'URI="%s"' % segment_url(cdn_url(value))

    body = re.sub(r'URI="([^"]+)"', attr_url, body)
    return re.sub(r"^[^\s#].*$", line_url, body, flags=re.M)


def remux_to_ts(data):
    """Turn one CMAF/fMP4 fragment into an MPEG-TS segment.

    The fragment alone cannot be demuxed: its moof box references track ids
    that only the init segment (the moov) defines. The init segment is
    therefore prepended to the fragment bytes, and ffmpeg is asked to copy
    both into MPEG-TS without touching the elementary streams.

    The copy keeps the original timestamps (-copyts), so a run of remuxed
    fragments lands on one continuous timeline and the TV player concatenates
    them without a gap. Each output carries its own PAT/PMT, which is what
    an HLS client expects from a segment.
    """
    if not os.path.exists(FFMPEG):
        return None
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             "-copyts", "-i", "pipe:0",
             "-c", "copy", "-muxdelay", "0", "-muxpreload", "0",
             "-f", "mpegts", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=REMUX_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


# A TS session is one title's segment list, kept so the manifest can be handed
# out again and the segments fetched by index. The TV plays a title in real
# time, so a session has to outlive the title itself: two hours plus slack.
_ts_sessions = {}
_TS_SESSION_TTL = 6 * 3600
_TS_SESSION_MAX = 32

# Remuxed segments are kept in memory for a short while. A player that gives
# up on a segment and asks for it again is common enough on these TVs, and
# the remux is not worth repeating.
_ts_segment_cache = {}
_ts_segment_order = []
_TS_SEGMENT_CACHE_MAX = 12

_ts_init_cache = {}

# One fetch per segment, even when the player and the prefetcher ask for the
# same one at the same moment: the second caller waits for the first and then
# reads the result out of the cache instead of pulling the fragment twice.
_ts_fetch_locks = {}


def _fetch_lock(url):
    with _lock:
        found = _ts_fetch_locks.get(url)
        if found is None:
            found = threading.Lock()
            _ts_fetch_locks[url] = found
        return found


def _ts_cache_get(key):
    with _lock:
        return _ts_segment_cache.get(key)


def _ts_cache_put(key, value):
    with _lock:
        if key in _ts_segment_cache:
            _ts_segment_order.remove(key)
        _ts_segment_cache[key] = value
        _ts_segment_order.append(key)
        while len(_ts_segment_order) > _TS_SEGMENT_CACHE_MAX:
            _ts_segment_cache.pop(_ts_segment_order.pop(0), None)


def ts_init_bytes(url):
    """Fetch the CMAF init segment (the moov) once and keep it.

    It is 1.3 KB and identical for every segment of a title, so fetching it
    once per title instead of once per segment keeps the segment path cheap.
    """
    if not url:
        return b""
    with _lock:
        hit = _ts_init_cache.get(url)
    if hit is not None:
        return hit
    try:
        data = http_get(url)
    except Exception:  # noqa: BLE001 - a failed init is reported downstream
        return b""
    with _lock:
        if len(_ts_init_cache) > 64:
            _ts_init_cache.clear()
        _ts_init_cache[url] = data
    return data


def ts_token(key):
    """The session token a title is addressed by in /tsseg/<token>/<n>.ts.

    It is derived from the title's cache key, so the same title always gets
    the same token: a prefetch and a later request land on the same session.
    """
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def ts_manifest(playlist, public_base, key):
    """Build an MPEG-TS HLS manifest for a title.

    The upstream playlist is CMAF: an init segment (EXT-X-MAP) plus a long run
    of fMP4 fragments. The players on the TV ignore EXT-X-MAP, so they never
    fetch the init and never decode a frame. The manifest handed to them
    therefore lists MPEG-TS segments, produced from the very same fragments.
    """
    body = playlist_body(playlist)
    base = playlist.rsplit("/", 1)[0] + "/"

    def cdn_url(value):
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("/"):
            origin = playlist.split("/", 3)
            return "/".join(origin[:3]) + value
        return base + value

    init_url = ""
    segments = []
    duration = 0.0
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_url = cdn_url(match.group(1))
            continue
        if line.startswith("#EXTINF:"):
            match = re.search(r"([0-9.]+)", line.split(":", 1)[1])
            duration = float(match.group(1)) if match else 0.0
            continue
        if line.startswith("#"):
            continue
        segments.append((cdn_url(line), duration))

    if not segments:
        return None

    token = ts_token(key)
    with _lock:
        _ts_sessions[token] = {
            "init": init_url,
            "fmp4": bool(init_url),
            "segments": segments,
            "expires": time.time() + _TS_SESSION_TTL,
        }
        if len(_ts_sessions) > _TS_SESSION_MAX:
            oldest = min(_ts_sessions,
                         key=lambda k: _ts_sessions[k]["expires"])
            _ts_sessions.pop(oldest, None)

    target = int(max(d for _, d in segments) + 0.999)
    out = ["#EXTM3U", "#EXT-X-VERSION:3",
           "#EXT-X-TARGETDURATION:%d" % target,
           "#EXT-X-MEDIA-SEQUENCE:0"]
    for index, (_, dur) in enumerate(segments):
        out.append("#EXTINF:%.3f," % dur)
        # The segment URL is relative on purpose. This box serves the player
        # as well as the gateway, so a relative URL keeps the request on one
        # origin. The browser refuses a cross-origin manifest, and the
        # absolute LAN address is a different origin from the page.
        out.append("/tsseg/%s/%06d.ts" % (token, index))
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def ts_segment_bytes(token, index):
    """Return one MPEG-TS segment of a title, remuxing it if it has to be."""
    with _lock:
        session = _ts_sessions.get(token)
    if session is None:
        return None, None
    if index < 0 or index >= len(session["segments"]):
        return None, None

    cdn = session["segments"][index][0]
    cached = _ts_cache_get(cdn)
    if cached is not None:
        return cached, session

    # The lock keeps two requests for the same fragment from pulling it twice.
    # The second one waits here and finds the bytes in the cache below.
    with _fetch_lock(cdn):
        cached = _ts_cache_get(cdn)
        if cached is not None:
            return cached, session

        try:
            raw = http_get(cdn)
        except Exception:  # noqa: BLE001 - reported to the player as a failure
            return None, session

        if not session["fmp4"]:
            # Already MPEG-TS upstream: hand the bytes over untouched.
            _ts_cache_put(cdn, raw)
            return raw, session

        init = ts_init_bytes(session["init"])
        if not init:
            return None, session
        out = remux_to_ts(init + raw)
        if out is None:
            return None, session
        _ts_cache_put(cdn, out)
        return out, session


def ts_prefetch(token, count=2):
    """Pull the first segments of a title into the cache.

    The player asks for the manifest and then, a moment later, for the first
    segment. Fetching that segment here, while the player is still reading
    the manifest, turns the wait for the first picture into the time the
    player needs to decode what is already in the buffer.
    """
    for index in range(count):
        try:
            ts_segment_bytes(token, index)
        except Exception:  # noqa: BLE001 - a prefetch failure is not an error
            return


def playlist_body(playlist):
    """Fetch the upstream playlist, with a short cache.

    Every request for the same title carries the same signed playlist URL, and
    the segment signatures inside the playlist are the ones of that URL, so a
    cached copy stays valid for the lifetime of the signature.
    """
    now = time.time()
    with _lock:
        entry = _playlist_cache.get(playlist)
        if entry and entry[0] > now:
            return entry[1]
    body = http_get(playlist).decode("utf-8", "replace")
    with _lock:
        _playlist_cache[playlist] = (now + _PLAYLIST_TTL, body)
        while len(_playlist_cache) > _PLAYLIST_MAX:
            oldest = next(iter(_playlist_cache))
            _playlist_cache.pop(oldest, None)
    return body


def segment_content_type(path):
    """Content type for a segment alias, from the extension it carries."""
    if path.endswith(".ts"):
        return "video/mp2t"
    if path.endswith(".mp4"):
        return "video/mp4"
    return "video/mp4"


# The covers come from the image host on the internet. The browser on the
# television pulls sixty of them at once, over sixty connections to a host
# that is not on the LAN, and the grid stays empty until they arrive. The box
# fetches each cover once and keeps it on disk; after that the television
# reads them off the LAN.
POSTER_DIR = os.environ.get("RESOLVER_POSTER_DIR",
                           os.path.join(CACHE_DIR, "posters"))
POSTER_SOURCE = "https://image.tmdb.org/t/p/w342/"
_POSTER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_poster_locks = {}
_poster_slots = threading.Semaphore(4)


def _poster_lock(name):
    with _lock:
        found = _poster_locks.get(name)
        if found is None:
            found = threading.Lock()
            _poster_locks[name] = found
        return found


def fetch_poster(name, path):
    """Fetch one cover from the image host and keep it on disk."""
    with _poster_lock(name):
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            pass

        url = POSTER_SOURCE + name
        try:
            with _poster_slots:
                request = urllib.request.Request(
                    url, headers={"User-Agent": UA, "Accept": "image/*"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = response.read()
        except Exception:  # noqa: BLE001 - the page falls back to a placeholder
            return None
        if not data:
            return None

        try:
            os.makedirs(POSTER_DIR, exist_ok=True)
            partial = path + ".part"
            with open(partial, "wb") as handle:
                handle.write(data)
            os.replace(partial, path)
        except OSError:
            pass
        return data


def stream_route(parts):
    """Return (cache key, resolver) for a /stream or /check path.

    Both routes share the same shape - /stream/movie/<id>,
    /stream/tv/<id>/<season>/<episode>, /stream/imdb/<id> - only the verb
    differs, so one table serves both.
    """
    verb = parts[0] if parts else ""
    if verb not in ("stream", "check"):
        return None
    if len(parts) >= 3 and parts[1] == "movie":
        title = parts[2]
        return "movie:" + title, lambda: resolve_page(
            "%s/movie/%s" % (WATCHPLAY, title))
    if len(parts) >= 3 and parts[1] == "imdb":
        title = parts[2]
        return "movie:" + title, lambda: resolve_page(
            "%s/movie/%s" % (WATCHPLAY, title))
    if len(parts) >= 5 and parts[1] == "tv":
        title, season, episode = parts[2], parts[3], parts[4]
        return "tv:%s:%s:%s" % (title, season, episode), lambda: resolve_page(
            "%s/tvshow/%s/%s/%s" % (WATCHPLAY, title, season, episode))
    return None


def tv_route(kind, ident, season="1", episode="1"):
    """Return (cache key, resolver) for a title the TV plays.

    The manifest route and the prefetcher share this, so both land on the
    same cache entry and on the same session token.
    """
    if kind == "movie" and ident:
        return ("movie:" + ident,
                lambda: resolve_page("%s/movie/%s" % (WATCHPLAY, ident)))
    if kind == "tv" and ident:
        return ("tv:%s:%s:%s" % (ident, season, episode),
                lambda: resolve_page(
                    "%s/tvshow/%s/%s/%s" % (WATCHPLAY, ident, season, episode)))
    return None


def warm_tv_title(kind, ident, season="1", episode="1"):
    """Resolve a title and pull its first segments before the player asks.

    The player page is served the moment a title is opened. By the time the
    viewer presses OK the manifest and the first segments are already in
    place, so the press finds a buffer that is ready and the picture comes up
    without another round trip to the upstream host.
    """
    route = tv_route(kind, ident, season, episode)
    if route is None:
        return
    key, resolver = route
    try:
        playlist, _ = cached_get(key, resolver)
    except Exception:  # noqa: BLE001 - a warm-up failure is not an error
        return
    if not playlist:
        return
    try:
        ts_manifest(playlist, default_public_base(), key)
    except Exception:  # noqa: BLE001 - the manifest is built again on request
        return
    ts_prefetch(ts_token(key), 2)


class Handler(BaseHTTPRequestHandler):
    server_version = "CineStreamResolver/1.0"

    # HTTP/1.1 with a Content-Length on every answer keeps the connection
    # open. The browser on the television opens six connections per host and
    # holds them for the whole title; with HTTP/1.0 every segment cost a new
    # TCP handshake.
    protocol_version = "HTTP/1.1"
    timeout = 30

    # Text answers are compressed. The catalog is seven megabytes of JSON and
    # the browser on the television parses every byte of it before the first
    # cover shows up.
    COMPRESSIBLE = (
        "text/", "application/javascript", "application/json",
        "image/svg+xml", "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
    )

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _wants_gzip(self):
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def _send(self, code, body=b"", headers=None):
        headers = dict(headers or {})
        content_type = headers.get("Content-Type", "")
        if (body and len(body) > 512 and self._wants_gzip()
                and content_type.startswith(self.COMPRESSIBLE)):
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        if CORS_ORIGIN:
            headers.setdefault("Access-Control-Allow-Origin", CORS_ORIGIN)
            headers.setdefault("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            headers.setdefault("Access-Control-Allow-Headers", "*")
            headers.setdefault("Access-Control-Max-Age", "600")
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self._send(code, body, {"Content-Type": "application/json"})

    def do_GET(self):
        raw_path = self.path
        path = self.path.split("?", 1)[0].rstrip("/")
        parts = [p for p in path.split("/") if p]

        if path == "/cinestream.m3u":
            try:
                with open(M3U_PATH, "rb") as f:
                    m3u_data = f.read()
                self._send(200, m3u_data, {"Content-Type": "application/x-mpegurl; charset=utf-8"})
            except Exception as e:
                self._json(504, {"error": "m3u not generated yet: " + str(e)})
            return

        if path == "/play":
            # Parse query parameters manually for maximum compatibility
            query_string = urllib.parse.urlparse(raw_path).query
            params = urllib.parse.parse_qs(query_string)
            ty = params.get('ty', ['movie'])[0]
            tm = params.get('tm', [''])[0]
            title = params.get('title', ['Assistir'])[0]
            se = params.get('se', ['1'])[0]
            ep = params.get('ep', ['1'])[0]

            if ty == 'movie':
                stream_url = '/tv/movie/' + tm
            else:
                stream_url = '/tv/tv/' + tm + '/' + se + '/' + ep

            body = ("""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Assistir - """ + title + """</title>
    <style>
        body { background: #000; color: #fff; font-family: sans-serif; margin: 0; padding: 0; overflow: hidden; }
        video { width: 100vw; height: 100vh; object-fit: contain; background: #000; position: fixed; top: 0; left: 0; z-index: 1; outline: none; }
        #overlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); z-index: 10; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; }
        h1 { color: #fff; font-size: 28px; margin-bottom: 20px; text-align: center; padding: 0 20px; }
        .btn-play { background: #e50914; color: #fff; border: none; padding: 15px 40px; font-size: 22px; font-weight: bold; border-radius: 6px; cursor: pointer; box-shadow: 0 4px 15px rgba(229,9,20,0.5); }
        .btn-play:focus, .btn-play:hover { background: #ff1e2a; outline: 3px solid #fff; }
        .back-link { position: fixed; top: 20px; left: 20px; z-index: 20; background: rgba(0,0,0,0.6); color: #fff; padding: 8px 16px; border-radius: 4px; text-decoration: none; font-size: 14px; border: 1px solid #444; }
    </style>
    <script src="/js/hls.min.js"></script>
</head>
<body>
    <a class="back-link" href="/minatv">&#9664; Voltar</a>
    
    <div id="overlay" onclick="startPlay()">
        <h1>""" + title + """</h1>
        <button class="btn-play">▶ CLIQUE PARA ASSISTIR</button>
    </div>

    <video id="player" controls playsinline></video>

    <script>
        var video = document.getElementById('player');
        var overlay = document.getElementById('overlay');
        var sourceUrl = '""" + stream_url + """';
        var hlsLoaded = false;

        function startPlay() {
            overlay.style.display = 'none';
            if (!hlsLoaded) {
                hlsLoaded = true;
                if (window.Hls && Hls.isSupported()) {
                    var hls = new Hls();
                    hls.loadSource(sourceUrl);
                    hls.attachMedia(video);
                    hls.on(Hls.Events.MANIFEST_PARSED, function() {
                        video.play().catch(function(e){ console.log("Play error:", e); });
                    });
                    hls.on(Hls.Events.ERROR, function(event, data) {
                        console.log("HLS error:", data);
                    });
                } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                    video.src = sourceUrl;
                    video.load();
                    video.play().catch(function(e){ console.log("Play error:", e); });
                } else {
                    video.src = sourceUrl;
                    video.play();
                }
            } else {
                video.play();
            }
            
            // Tentar tela cheia
            if (video.requestFullscreen) {
                video.requestFullscreen().catch(function(){});
            } else if (video.webkitRequestFullscreen) {
                video.webkitRequestFullscreen();
            }
        }

        // Focar no botão ao carregar para o controle remoto da TV selecionar
        window.onload = function() {
            var btn = document.querySelector('.btn-play');
            if (btn) btn.focus();
        };

        // Controle remoto SRAF
        document.addEventListener('keydown', function(e) {
            var key = e.keyCode || e.which;
            if (key === 403 || key === 461 || key === 27) { // Vermelho / Back / ESC
                window.location.href = '/minatv';
            }
        });
    </script>
</body>
</html>""").encode("utf-8")
            self._send(200, body, {"Content-Type": "text/html; charset=utf-8"})
            return

        if path == "/minatv":
            body = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mina TV - CineStream (TV Mode)</title>
    <style>
        body { background: #0b0d10; color: #fff; font-family: sans-serif; margin: 0; padding: 20px; }
        h1 { color: #e50914; margin-top: 0; font-size: 28px; }
        .nav-bar { margin-bottom: 20px; }
        .btn-tab { background: #222; color: #fff; border: 2px solid #444; padding: 10px 20px; font-size: 16px; cursor: pointer; margin-right: 10px; border-radius: 4px; }
        .btn-tab.active { background: #e50914; border-color: #ff4a52; }
        #grid { display: flex; flex-wrap: wrap; gap: 15px; }
        .card { width: 150px; background: #161a22; border: 2px solid #333; border-radius: 6px; overflow: hidden; cursor: pointer; text-decoration: none; color: inherit; display: block; }
        .card:focus, .card:hover { border-color: #e50914; outline: none; transform: scale(1.05); }
        .card img { width: 100%; height: 220px; object-fit: cover; background: #000; }
        .card-title { padding: 8px; font-size: 13px; font-weight: bold; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 0; }
        #status-bar { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.9); padding: 10px 20px; font-size: 14px; border-top: 1px solid #333; z-index: 999; }
        .key-red { color: #ff4444; font-weight: bold; }
        .key-green { color: #44ff44; font-weight: bold; }
    </style>
</head>
<body>
    <h1>CINESTREAM — TV MODE</h1>
    <div class="nav-bar">
        <button class="btn-tab active" id="tab-movies" onclick="switchCat('movies')">Filmes</button>
        <button class="btn-tab" id="tab-series" onclick="switchCat('series')">Séries</button>
        <button class="btn-tab" id="tab-animes" onclick="switchCat('animes')">Animes</button>
    </div>
    <div id="grid">Carregando catálogo...</div>
    
    <div id="status-bar">
        <span class="key-red">[VERMELHO / BACK]</span> Recarregar | <span class="key-green">[VERDE]</span> Ir para o Site Oficial
    </div>

    <script>
        var currentCat = 'movies';
        var files = {
            movies: '/cat/movies.json',
            series: '/cat/series.json',
            animes: '/cat/animes.json'
        };

        function switchCat(cat) {
            currentCat = cat;
            document.getElementById('tab-movies').className = 'btn-tab' + (cat === 'movies' ? ' active' : '');
            document.getElementById('tab-series').className = 'btn-tab' + (cat === 'series' ? ' active' : '');
            document.getElementById('tab-animes').className = 'btn-tab' + (cat === 'animes' ? ' active' : '');
            loadCatalog(files[cat], cat);
        }

        function loadCatalog(url, type) {
            var grid = document.getElementById('grid');
            grid.innerHTML = 'Carregando ' + type + '...';
            var xhr = new XMLHttpRequest();
            xhr.open('GET', url, true);
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    if (xhr.status === 200) {
                        try {
                            var data = JSON.parse(xhr.responseText);
                            renderItems(data.items || [], type);
                        } catch (e) {
                            grid.innerHTML = 'Erro ao processar JSON: ' + e.message;
                        }
                    } else {
                        grid.innerHTML = 'Erro HTTP ' + xhr.status + ' ao carregar dados.';
                    }
                }
            };
            xhr.send();
        }

        function renderItems(items, type) {
            var grid = document.getElementById('grid');
            grid.innerHTML = '';
            var max = Math.min(items.length, 60);
            for (var i = 0; i < max; i++) {
                var item = items[i];
                var a = document.createElement('a');
                a.className = 'card';
                a.href = '/play?ty=' + (type === 'movies' ? 'movie' : type) + '&title=' + encodeURIComponent(item.t || '') + '&tm=' + (item.tm || '') + '&se=1&ep=1';
                a.target = '_blank';
                
                var img = document.createElement('img');
                var poster = item.p;
                if (poster && poster.indexOf('/') === 0) {
                    poster = 'https://image.tmdb.org/t/p/w342' + poster;
                } else {
                    poster = 'https://aalexaalmeida108-cmd.github.io/cinestream-br-v2/assets/placeholder.svg';
                }
                img.src = poster;
                img.onerror = function() { this.src = 'https://aalexaalmeida108-cmd.github.io/cinestream-br-v2/assets/placeholder.svg'; };
                
                var title = document.createElement('p');
                title.className = 'card-title';
                title.textContent = item.t || 'Sem Título';
                
                a.appendChild(img);
                a.appendChild(title);
                grid.appendChild(a);
            }
            if (grid.children.length > 0) {
                grid.children[0].focus();
            }
        }

        // Controle remoto SRAF
        document.addEventListener('keydown', function(e) {
            var key = e.keyCode || e.which;
            if (key === 403 || key === 461 || key === 27) { // Vermelho ou Back
                location.reload();
            }
            if (key === 404) { // Verde
                window.location.href = 'https://aalexaalmeida108-cmd.github.io/cinestream-br-v2/';
            }
        });

        // Iniciar
        switchCat('movies');
    </script>
</body>
</html>""".encode("utf-8")
            self._send(200, body, {"Content-Type": "text/html; charset=utf-8"})
            return

        if path == "/healthz":
            with _lock:
                self._json(200, {
                    "status": "ok",
                    "mode": MODE,
                    "public_base": public_base_for(self.headers.get("Host")),
                    "lan_ip": lan_ip(),
                    "cached": len(_cache),
                    "stats": dict(_stats),
                    "now": int(time.time()),
                })
            return

        if len(parts) >= 2 and parts[0] == "seg":
            self.serve_segment(raw_path)
            return

        if len(parts) == 2 and parts[0] == "cat":
            self.serve_catalog(parts[1])
            return

        if len(parts) == 2 and parts[0] == "js":
            self.serve_asset(parts[1])
            return

        if len(parts) == 2 and parts[0] == "poster":
            self.serve_poster(parts[1])
            return

        if path == "/site" or path.startswith("/site/"):
            self.serve_site(parts[1:])
            return

        if len(parts) >= 3 and parts[0] == "tsseg":
            self.serve_ts_segment(parts)
            return

        if len(parts) >= 2 and parts[0] == "tv":
            self.serve_tv(parts)
            return

        # Plex decides how to probe a .strm from the file name it points at, so
        # the id may carry a media suffix (.m3u8, .mp4, ...). Strip it before
        # looking the title up upstream.
        if parts and parts[-1].lower().endswith((".m3u8", ".mp4", ".mkv", ".ts")):
            parts[-1] = parts[-1].rsplit(".", 1)[0]

        if len(parts) >= 2 and parts[0] == "check":
            self.serve_check(parts)
            return

        route = stream_route(parts)
        if route is None:
            self._json(404, {"error": "unknown route", "path": self.path})
            return

        key, resolver = route
        try:
            playlist, _ = cached_get(key, resolver)
        except urllib.error.HTTPError as exc:
            with _lock:
                _stats["errors"] += 1
            if exc.code == 404:
                # The upstream host has no source for this title. That is a
                # fact about the title, not a hiccup, and the caller has to be
                # able to tell the two apart.
                self._json(404, {"error": "no playable source found",
                                 "status": 404})
                return
            self._json(502, {"error": "upstream http error", "status": exc.code})
            return
        except Exception as exc:  # noqa: BLE001 - report and keep serving
            with _lock:
                _stats["errors"] += 1
            self._json(502, {"error": str(exc)})
            return

        if not playlist:
            self._json(404, {"error": "no playable source found"})
            return

        if MODE == "proxy":
            try:
                base = public_base_for(self.headers.get("Host"))
                body = rewrite_manifest(playlist, base).encode("utf-8")
                with _lock:
                    _stats["proxied"] += 1
                self._send(200, body, {
                    "Content-Type": "application/vnd.apple.mpegurl",
                    "Cache-Control": "no-store",
                })
                return
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": "proxy failed: %s" % exc})
                return

        self._send(302, b"", {
            "Location": playlist,
            "Cache-Control": "no-store",
        })

    def do_OPTIONS(self):
        """Answer the preflight of a cross-origin request.

        Only sent when RESOLVER_CORS_ORIGIN is set. Without it the browser
        never asks, because the site and the gateway are on one host.
        """
        if not CORS_ORIGIN:
            self._send(405, b"", {"Content-Type": "text/plain"})
            return
        self._send(204, b"", {"Content-Type": "text/plain"})

    def do_HEAD(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/seg"):
            self._send(200, b"", {
                "Content-Type": segment_content_type(path),
                "Accept-Ranges": "bytes",
            })
            return
        if path.startswith("/tsseg"):
            self._send(200, b"", {
                "Content-Type": "video/mp2t",
                "Accept-Ranges": "bytes",
            })
            return
        self._send(200, b"", {"Content-Type": "application/vnd.apple.mpegurl"})

    def serve_check(self, parts):
        """Report whether a title has a playable source, without a body.

        ok        - the upstream host has a source for this title
        missing   - the upstream host has no source for this title (dead)
        transient - the upstream host failed for now (timeout, 403, 5xx)

        The distinction is the whole point of this route: a title that is dead
        upstream stays dead, a title that hiccuped upstream must be probed
        again later instead of being written off.
        """
        route = stream_route(parts)
        if route is None:
            self._json(404, {"error": "unknown route", "path": self.path})
            return
        key, resolver = route
        try:
            playlist, _ = cached_get(key, resolver)
        except urllib.error.HTTPError as exc:
            status = "missing" if exc.code == 404 else "transient"
            self._json(200, {"status": status, "upstream": exc.code})
            return
        except Exception as exc:  # noqa: BLE001 - report and keep serving
            self._json(200, {"status": "transient", "error": str(exc)})
            return
        self._json(200, {"status": "ok" if playlist else "missing"})

    def serve_site(self, parts):
        """Serve the TV edition of the site from this box.

        The public site hands the player out as fragmented MP4 inside an
        iframe, and the browser on the TV cannot decode that. This copy is
        plain ES5 with a native video element, and it points at the MPEG-TS
        gateway. It lives on the box rather than on the public host for two
        reasons: the catalog is 7 MB per category and the TV should not need
        the internet for it, and an HTTP page may load an HTTP video without
        the mixed-content rules getting in the way.
        """
        root = SITE_DIR
        relative = "/".join(parts)
        if not relative or relative.endswith("/"):
            relative += "index.html"
        # No traversal out of the site directory.
        path = os.path.normpath(os.path.join(root, relative))
        if not path.startswith(root + os.sep) and path != root:
            self._json(403, {"error": "forbidden"})
            return
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._json(404, {"error": "not found", "path": relative})
            return

        # The player page names the title in its query string. That is the
        # moment to get the title ready: the manifest and the first segments
        # are fetched and remuxed while the viewer reaches for the remote.
        if relative.endswith("player.html"):
            self._warm_from_query()

        types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
        }
        extension = os.path.splitext(path)[1].lower()
        self._send(200, body, {
            "Content-Type": types.get(extension, "application/octet-stream"),
            "Cache-Control": "max-age=600",
        })

    def _warm_from_query(self):
        """Start the prefetch of the title the player page is about to open."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        kind = (query.get("ty") or ["movie"])[0]
        ident = (query.get("tm") or [""])[0]
        season = (query.get("se") or ["1"])[0]
        episode = (query.get("ep") or ["1"])[0]
        if not ident:
            return
        thread = threading.Thread(
            target=warm_tv_title, args=(kind, ident, season, episode))
        thread.daemon = True
        thread.start()

    def serve_poster(self, name):
        """Serve one cover of the grid, kept on disk by the box."""
        if not _POSTER_NAME_RE.match(name):
            self._json(404, {"error": "bad poster name"})
            return
        path = os.path.join(POSTER_DIR, name)
        body = fetch_poster(name, path)
        if body is None:
            self._json(404, {"error": "poster unavailable"})
            return
        self._send(200, body, {
            "Content-Type": "image/jpeg",
            "Cache-Control": "max-age=604800",
        })

    def serve_asset(self, name):
        """Serve a static asset from vendor/js.

        The hls.js build is kept local on purpose. The TV's browser is old
        enough that a CDN outage would take the player down with it, and the
        file is 235 KB, which is nothing to keep on disk.
        """
        if name != "hls.min.js":
            self._json(404, {"error": "unknown asset", "name": name})
            return
        path = os.path.join(SITE_DIR, "js", "hls.min.js")
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self._json(404, {"error": str(exc)})
            return
        self._send(200, body, {
            "Content-Type": "application/javascript; charset=utf-8",
            "Cache-Control": "max-age=86400",
        })

    def serve_catalog(self, category):
        """Serve a catalog file from the local cache.

        The TV pulls its catalog from this box rather than from the public
        site: the catalog is 7 MB per category, and the public host is not
        always reachable from the living room.
        """
        if category.endswith(".json"):
            category = category[:-len(".json")]
        if category not in ("movies", "series", "animes"):
            self._json(404, {"error": "unknown category", "category": category})
            return
        path = os.path.join(CATALOG_DIR, "%s.json" % category)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self._json(404, {"error": str(exc)})
            return
        self._send(200, body, {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "max-age=3600",
        })

    def serve_ts_segment(self, parts):
        """Serve one MPEG-TS segment of a title listed by /tv/."""
        token = parts[1]
        try:
            index = int(parts[2].split(".")[0])
        except (ValueError, IndexError):
            self._json(400, {"error": "bad segment index"})
            return
        data, _ = ts_segment_bytes(token, index)
        if data is None:
            self._json(502, {"error": "segment unavailable", "index": index})
            return
        self._send(200, data, {
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-store",
        })

    def serve_tv(self, parts):
        """Serve the MPEG-TS manifest of a title for the TV player."""
        kind = parts[1]
        if kind == "movie" and len(parts) >= 3:
            route = tv_route("movie", parts[2])
        elif kind == "tv" and len(parts) >= 5:
            route = tv_route("tv", parts[2], parts[3], parts[4])
        else:
            route = None
        if route is None:
            self._json(404, {"error": "unknown tv route", "path": self.path})
            return
        key, resolver = route

        try:
            playlist, _ = cached_get(key, resolver)
        except urllib.error.HTTPError as exc:
            self._json(404 if exc.code == 404 else 502,
                       {"error": "no playable source found", "status": exc.code})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": str(exc)})
            return

        if not playlist:
            self._json(404, {"error": "no playable source found"})
            return

        base = public_base_for(self.headers.get("Host"))
        try:
            body = ts_manifest(playlist, base, key)
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": str(exc)})
            return
        if not body:
            self._json(404, {"error": "no playable source found"})
            return

        with _lock:
            _stats["proxied"] += 1
        self._send(200, body.encode("utf-8"), {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-store",
        })

    def serve_segment(self, raw_path):
        """Stream one CDN segment back, keeping range requests intact."""
        query = urllib.parse.urlparse(raw_path).query
        target = urllib.parse.parse_qs(query).get("u", [""])[0]
        if not target.startswith("http"):
            self._json(400, {"error": "missing segment url"})
            return
        path = urllib.parse.urlparse(raw_path).path
        try:
            request = urllib.request.Request(target, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Range": self.headers.get("Range", ""),
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                # The segment is copied out in pieces: a whole title would
                # otherwise sit in memory once per open connection.
                length = response.headers.get("Content-Length")
                self.send_response(response.status)
                self.send_header("Content-Type", segment_content_type(path))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if length:
                    self.send_header("Content-Length", length)
                else:
                    # Without a length the answer ends when the connection
                    # ends. Under HTTP/1.1 that has to be said out loud,
                    # or the client waits for bytes that never come.
                    self.close_connection = True
                content_range = response.headers.get("Content-Range")
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as exc:
            self._send(exc.code, b"", {"Content-Type": "text/plain"})
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": str(exc)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log("resolver listening on %s:%d mode=%s" % (HOST, PORT, MODE))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
