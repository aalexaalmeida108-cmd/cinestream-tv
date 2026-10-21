#!/usr/bin/env python3
"""Bundle the covers the television needs into the cloud copy of the site.

The box keeps the covers it has seen under cache/posters/. The cloud copy
ships with them, so the television opens no second connection per card - the
reason the covers were moved onto the box in the first place.

A cover that is not on the box is fetched once from the image host and kept
in the bundle. The first grid page of every category is covered, because that
is what the television draws first; anything deeper falls back to the image
host at run time.

Usage:
    bundle_posters.py [--first 60] [--workers 8] [--out ../site/posters]
"""

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request

CATEGORIES = ("movies", "series", "animes")
CACHE_DIR = "/home/samba/plex/cache/posters"
IMAGE_HOST = "https://image.tmdb.org/t/p/w342/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def catalog_names(data_dir, first):
    """The poster names of the first titles of every category."""
    wanted = []
    for category in CATEGORIES:
        path = os.path.join(data_dir, category + ".json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for item in payload.get("items", [])[:first]:
            poster = item.get("p") or ""
            name = poster.lstrip("/")
            if name and name not in wanted:
                wanted.append(name)
    return wanted


def fetch(name, out_dir, lock, stats):
    target = os.path.join(out_dir, name)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        with lock:
            stats["kept"] += 1
        return True
    request = urllib.request.Request(
        IMAGE_HOST + name, headers={"User-Agent": UA, "Accept": "image/*"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except Exception as exc:  # noqa: BLE001 - a missing cover is not fatal
        with lock:
            stats["failed"] += 1
            stats["errors"].append("%s: %s" % (name, exc))
        return False
    if not body:
        with lock:
            stats["failed"] += 1
        return False
    tmp = target + ".part"
    with open(tmp, "wb") as handle:
        handle.write(body)
    os.replace(tmp, target)
    with lock:
        stats["fetched"] += 1
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "posters"))
    parser.add_argument("--first", type=int, default=60)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    # What the box already has travels with the bundle.
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            if not name.lower().endswith(".jpg"):
                continue
            source = os.path.join(CACHE_DIR, name)
            target = os.path.join(out_dir, name)
            if os.path.exists(target) and os.path.getsize(target) > 0:
                continue
            with open(source, "rb") as handle:
                body = handle.read()
            with open(target, "wb") as handle:
                handle.write(body)

    names = catalog_names(os.path.abspath(args.data_dir), args.first)
    stats = {"kept": 0, "fetched": 0, "failed": 0, "errors": []}
    lock = threading.Lock()
    pending = [name for name in names if not os.path.exists(os.path.join(out_dir, name))]
    print("capas: %d pedidas, %d ja no pacote, %d para buscar" % (
        len(names), len(names) - len(pending), len(pending)))

    index = [0]

    def worker():
        while True:
            with lock:
                if index[0] >= len(pending):
                    return
                name = pending[index[0]]
                index[0] += 1
            fetch(name, out_dir, lock, stats)

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = sum(os.path.getsize(os.path.join(out_dir, n)) for n in os.listdir(out_dir))
    print("pronto: %d no pacote, %d baixadas, %d falhas, %.1f MB" % (
        stats["kept"], stats["fetched"], stats["failed"], total / 1048576.0))
    for line in stats["errors"][:10]:
        print("  falha %s" % line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
