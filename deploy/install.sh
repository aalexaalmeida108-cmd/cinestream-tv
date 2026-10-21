#!/usr/bin/env bash
# CineStream TV - install the site and the gateway on a host in the cloud.
#
# The site is a pile of static files. The gateway is the resolver: it signs
# the title against the upstream host and hands out MPEG-TS manifests and
# segments. Caddy puts HTTPS in front and serves the site straight off the
# disk, so the browser on the television never makes a cross-origin request.
#
#   sudo ./install.sh --domain tv.example.com --email you@example.com
#
# The domain has to point at the public IP of the host before this runs,
# otherwise Caddy cannot get the certificate.
#
# Options:
#   --domain HOST      the domain the television opens (required)
#   --email MAIL       mail for the Let's Encrypt account (required)
#   --base-url URL     the public base of the gateway (default https://HOST)
#   --cors ORIGIN      allow the GitHub Pages copy of the site to call the
#                      gateway (default: nothing, the site and the gateway
#                      are on one host and the browser never asks)
#   --no-caddy         install the gateway only, no HTTPS in front
#   --ffmpeg PATH      ffmpeg binary (default: the one from the package
#                      manager)

set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/cinestream
LOG_DIR=/var/log/cinestream
SERVICE_USER=cinestream
DOMAIN=""
EMAIL=""
BASE_URL=""
CORS=""
USE_CADDY=1
FFMPEG=""

die() { echo "erro: $*" >&2; exit 1; }
say() { echo "==> $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="${2:-}"; shift 2 ;;
        --email) EMAIL="${2:-}"; shift 2 ;;
        --base-url) BASE_URL="${2:-}"; shift 2 ;;
        --cors) CORS="${2:-}"; shift 2 ;;
        --no-caddy) USE_CADDY=0; shift ;;
        --ffmpeg) FFMPEG="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "opcao desconhecida: $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "rode com sudo"
[ -n "$DOMAIN" ] || die "--domain e obrigatorio"
[ -n "$EMAIL" ] || die "--email e obrigatorio"
[ -d "$BUNDLE_ROOT/site" ] || die "o pacote do site nao esta em $BUNDLE_ROOT/site"
[ -f "$BUNDLE_ROOT/deploy/resolver.py" ] || die "o gateway nao esta em $BUNDLE_ROOT/deploy/resolver.py"

BASE_URL="${BASE_URL:-https://$DOMAIN}"

say "pacote: $BUNDLE_ROOT"
say "dominio: $DOMAIN   base: $BASE_URL"

# ---------------------------------------------------------------- packages
say "pacotes"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 ca-certificates curl gnupg apt-transport-https >/dev/null
if [ -z "$FFMPEG" ]; then
    apt-get install -y -qq ffmpeg >/dev/null
    FFMPEG="$(command -v ffmpeg)"
fi
say "ffmpeg: $FFMPEG"

if [ "$USE_CADDY" -eq 1 ]; then
    if ! command -v caddy >/dev/null 2>&1; then
        say "instalando caddy"
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        apt-get update -qq
        apt-get install -y -qq caddy >/dev/null
    fi
fi

# ------------------------------------------------------------------- user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "usuario $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "pastas"
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/site" "$INSTALL_DIR/cache" "$INSTALL_DIR/posters" "$LOG_DIR" /etc/cinestream
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$LOG_DIR"

say "copiando o site"
rsync -a --delete "$BUNDLE_ROOT/site/" "$INSTALL_DIR/site/"
cp -a "$BUNDLE_ROOT/deploy/resolver.py" "$INSTALL_DIR/resolver.py"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 0755 "$INSTALL_DIR/resolver.py"

# -------------------------------------------------------------------- env
say "configuracao"
cat > /etc/cinestream/env <<EOF
RESOLVER_HOST=127.0.0.1
RESOLVER_PORT=8730
RESOLVER_MODE=proxy
RESOLVER_CACHE_TTL=300
RESOLVER_HOME=$INSTALL_DIR
RESOLVER_SITE_DIR=$INSTALL_DIR/site
RESOLVER_CATALOG_DIR=$INSTALL_DIR/site/data
RESOLVER_POSTER_DIR=$INSTALL_DIR/posters
RESOLVER_M3U_PATH=$INSTALL_DIR/cinestream.m3u
RESOLVER_FFMPEG=$FFMPEG
RESOLVER_PUBLIC_BASE=$BASE_URL
RESOLVER_CORS_ORIGIN=$CORS
EOF
chmod 0640 /etc/cinestream/env
chown root:"$SERVICE_USER" /etc/cinestream/env

# ---------------------------------------------------------------- service
say "servico"
install -m 0644 "$BUNDLE_ROOT/deploy/cinestream-resolver.service" /etc/systemd/system/cinestream-resolver.service
systemctl daemon-reload
systemctl enable --now cinestream-resolver
sleep 1
systemctl --no-pager --full status cinestream-resolver | head -12 || true

# ------------------------------------------------------------------ caddy
if [ "$USE_CADDY" -eq 1 ]; then
    say "caddy"
    sed -e "s/{\$TV_DOMAIN}/$DOMAIN/" "$BUNDLE_ROOT/deploy/Caddyfile" > /etc/caddy/Caddyfile
    # Caddy takes the domain from the environment. The package reads
    # /etc/caddy/caddy.env; the drop-in below makes sure it does.
    printf 'TV_DOMAIN=%s\n' "$DOMAIN" > /etc/caddy/caddy.env
    mkdir -p /etc/systemd/system/caddy.service.d
    cat > /etc/systemd/system/caddy.service.d/override.conf <<EOF
[Service]
EnvironmentFile=/etc/caddy/caddy.env
EOF
    systemctl daemon-reload
    systemctl reload caddy || systemctl restart caddy
fi

# --------------------------------------------------------------- checking
say "conferindo"
sleep 2
printf 'gateway : '
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8730/healthz"
printf 'site    : '
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8730/site/index.html"
if [ "$USE_CADDY" -eq 1 ]; then
    printf 'publico : '
    curl -s -o /dev/null -w '%{http_code}\n' "https://$DOMAIN/" || true
fi

cat <<EOF

pronto.

  a television abre   $BASE_URL/
  o gateway responde  $BASE_URL/tv/movie/<id>
  o log fica em       $LOG_DIR/resolver.log

o certificado do Caddy aparece em /var/log/caddy/caddy.log quando o
dominio ainda nao aponta para este host.
EOF
