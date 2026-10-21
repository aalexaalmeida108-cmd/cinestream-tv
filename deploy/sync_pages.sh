#!/usr/bin/env bash
# Keep the cloud copy of the site fresh.
#
# The catalog on the box changes every six hours. This script takes the lean
# copy the box writes, drops it into the cloud bundle, picks up the covers
# that are new, and pushes the lot to the GitHub Pages repository.
#
# Only data/ and posters/ travel. The pages and the config are the cloud
# copy and are never overwritten by the box.
#
# Usage:
#   ./sync_pages.sh [--no-push]

set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE="$BUNDLE_ROOT"
BOX_SITE="/home/samba/plex/site"
PUSH=1

while [ $# -gt 0 ]; do
    case "$1" in
        --no-push) PUSH=0; shift ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "opcao desconhecida: $1" >&2; exit 1 ;;
    esac
done

say() { echo "==> $*"; }

say "catalogo enxuto na caixa"
python3 /home/samba/plex/bin/slim_catalog.py

say "copiando data/"
mkdir -p "$SITE/data"
for file in "$BOX_SITE"/data/*.json; do
    cp -f "$file" "$SITE/data/"
done

say "capas"
python3 "$BUNDLE_ROOT/deploy/bundle_posters.py" --first 60 --workers 6

if [ "$PUSH" -eq 0 ]; then
    say "sem push"
    exit 0
fi

cd "$SITE"
if [ -z "$(git status --porcelain)" ]; then
    say "nada mudou"
    exit 0
fi
git add -A
git commit -m "chore: sync catalogo $(date -u +%Y-%m-%dT%H:%MZ)"
git push origin main
say "publicado"
