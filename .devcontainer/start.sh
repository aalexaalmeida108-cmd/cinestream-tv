#!/usr/bin/env bash
# Codespaces: sobe o gateway do CineStream TV.
#
# O codigo do repositorio e a propria maquina - o codespace e um container
# Linux com o repositorio dentro. O ffmpeg entra, o site vai para a pasta
# do gateway, o resolver sobe na porta 8730 e a porta vira publica em
# https://<codespace>-8730.app.github.dev.
#
# O log sobe para o proprio repositorio: e o unico canal de volta que
# existe de dentro do container para fora, e serve para ver o que falhou
# quando o codigo roda dentro de uma maquina que ninguem ve.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${HOME}/cinestream"
LOG="${HOME}/cinestream-start.log"
PORT=8730

mkdir -p "$HOME_DIR/cache" "$HOME_DIR/posters" "$HOME_DIR/site"

{
    echo "=== codespace start $(date -u +%FT%TZ) ==="
    echo "repo: $REPO"
    echo "user: $(id -un)"
    echo "python: $(command -v python3 || echo AUSENTE)"
    echo "ffmpeg: $(command -v ffmpeg || echo AUSENTE)"
    echo "rsync: $(command -v rsync || echo AUSENTE)"
    echo "curl: $(command -v curl || echo AUSENTE)"

    echo "--- ffmpeg"
    if ! command -v ffmpeg >/dev/null 2>&1; then
        echo "instalando ffmpeg"
        sudo apt-get update -qq 2>&1 | tail -3
        sudo apt-get install -y -qq ffmpeg 2>&1 | tail -3
    fi
    command -v ffmpeg || echo "ffmpeg NAO VEIO"

    echo "--- site"
    rm -rf "$HOME_DIR/site"
    mkdir -p "$HOME_DIR/site"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude '.git' --exclude '.github' --exclude '.devcontainer' \
            "$REPO/" "$HOME_DIR/site/" 2>&1 | tail -3
    else
        # Sem rsync: copia o que interessa, arquivo por arquivo.
        for item in index.html player.html data js css assets posters deploy; do
            [ -e "$REPO/$item" ] && cp -a "$REPO/$item" "$HOME_DIR/site/" 2>&1 | tail -2
        done
    fi
    ls -la "$HOME_DIR/site" | head -12

    echo "--- gateway"
    cp -f "$REPO/deploy/resolver.py" "$HOME_DIR/resolver.py"
    chmod 0755 "$HOME_DIR/resolver.py"

    cat > "$HOME_DIR/env.sh" <<EOF
export RESOLVER_HOME=$HOME_DIR
export RESOLVER_SITE_DIR=$HOME_DIR/site
export RESOLVER_CATALOG_DIR=$HOME_DIR/site/data
export RESOLVER_POSTER_DIR=$HOME_DIR/posters
export RESOLVER_M3U_PATH=$HOME_DIR/cinestream.m3u
export RESOLVER_FFMPEG=$(command -v ffmpeg || echo /usr/bin/ffmpeg)
export RESOLVER_HOST=0.0.0.0
export RESOLVER_PORT=$PORT
export RESOLVER_MODE=proxy
export RESOLVER_CACHE_TTL=300
export RESOLVER_CORS_ORIGIN=*
EOF
    cat "$HOME_DIR/env.sh"

    echo "--- subindo"
    pkill -f "$HOME_DIR/resolver.py" >/dev/null 2>&1
    sleep 1
    setsid bash -c "source $HOME_DIR/env.sh; exec python3 $HOME_DIR/resolver.py" \
        </dev/null >"$HOME_DIR/resolver.log" 2>&1 &
    sleep 4
    echo "resolver log:"
    tail -12 "$HOME_DIR/resolver.log" 2>&1
    echo "healthz:"
    curl -s -m 10 -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$PORT/healthz" 2>&1
    curl -s -m 10 "http://127.0.0.1:$PORT/healthz" 2>&1; echo
} >> "$LOG" 2>&1

# O log volta para o repositorio: e o jeito de ver o que aconteceu la
# dentro sem olhar para a tela do codespace.
cp -f "$LOG" "$REPO/cinestream-start.log" 2>/dev/null
(
    cd "$REPO"
    git add -f cinestream-start.log >/dev/null 2>&1
    git -c user.name=codespace -c user.email=codespace@users.noreply.github.com \
        commit -q -m "chore: log do codespace" >/dev/null 2>&1
    git push -q origin HEAD:main >/dev/null 2>&1
) || true

tail -40 "$LOG"
