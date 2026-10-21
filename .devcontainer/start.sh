#!/usr/bin/env bash
# Codespaces: sobe o gateway do CineStream TV.
#
# O codigo do repositorio e a propria maquina - o codigo space e um
# container Linux com o repositorio dentro. O ffmpeg entra, o site vai para
# /opt/cinestream/site, o resolver sobe na porta 8730 e a porta fica
# publica em https://<codespace>-8730.app.github.dev.
#
# O resolver descobre o endereco publico pelo cabecalho Host, entao a
# playlist aponta para a URL publica e nao para o 127.0.0.1 do container.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> ffmpeg"
if ! command -v ffmpeg >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq ffmpeg
fi

echo "==> gateway"
sudo mkdir -p /opt/cinestream/cache /opt/cinestream/posters /opt/cinestream/site
sudo cp -f deploy/resolver.py /opt/cinestream/resolver.py
sudo chmod 0755 /opt/cinestream/resolver.py
sudo rsync -a --delete --exclude '.git' --exclude '.github' --exclude '.devcontainer' \
    ./ /opt/cinestream/site/

cat > /opt/cinestream/env.sh <<'EOF'
export RESOLVER_HOME=/opt/cinestream
export RESOLVER_SITE_DIR=/opt/cinestream/site
export RESOLVER_CATALOG_DIR=/opt/cinestream/site/data
export RESOLVER_POSTER_DIR=/opt/cinestream/posters
export RESOLVER_M3U_PATH=/opt/cinestream/cinestream.m3u
export RESOLVER_FFMPEG=/usr/bin/ffmpeg
export RESOLVER_HOST=0.0.0.0
export RESOLVER_PORT=8730
export RESOLVER_MODE=proxy
export RESOLVER_CACHE_TTL=300
export RESOLVER_CORS_ORIGIN=*
EOF

echo "=> subindo o resolver"
pkill -f '/opt/cinestream/resolver.py' >/dev/null 2>&1 || true
sudo chmod 0755 /opt/cinestream/env.sh
sudo setsid bash -c 'source /opt/cinestream/env.sh; exec python3 /opt/cinestream/resolver.py' \
    </dev/null >/opt/cinestream/resolver.log 2>&1 &
sleep 3
curl -s -o /dev/null -w 'gateway: %{http_code}\n' http://127.0.0.1:8730/healthz || true
echo "==> pronto. a porta 8730 e publica em https://<codespace>-8730.app.github.dev"
