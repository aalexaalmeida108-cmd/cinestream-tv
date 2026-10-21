#!/usr/bin/env bash
# Codespaces: levanta o gateway do CineStream TV dentro do GitHub.
#
# Cria o codespace, espera o gateway subir dentro dele e mostra o endereco
# publico. A porta 8730 precisa ser passada para "Public" na aba Ports do
# codespace - essa e a unica acao que depende de um clique humano, porque
# essa conta do GitHub nao aceita login por senha (passkey) e a API de
# portas nao existe na API publica.
#
# Uso:
#   ./codespace.sh                 cria e mostra
#   ./codespace.sh --delete NOME   apaga um codespace
#   ./codespace.sh --list          lista os codespaces
#
# O token vem de $GITHUB_TOKEN ou de ~/.git-credentials.

set -uo pipefail

REPO="${CINE_REPO:-aalexaalmeida108-cmd/cinestream-tv}"
MACHINE="${CINE_MACHINE:-basicLinux32gb}"
PORT=8730

say() { echo "==> $*"; }

token() {
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        echo "$GITHUB_TOKEN"
        return
    fi
    python3 - <<'PY'
import re
try:
    line = open('/home/samba/.git-credentials').read().strip()
    m = re.search(r'//[^:]+:([^@]+)@', line)
    print(m.group(1) if m else '')
except OSError:
    print('')
PY
}

TOKEN="$(token)"
[ -n "$TOKEN" ] || { echo "sem token: use GITHUB_TOKEN ou ~/.git-credentials" >&2; exit 1; }

api() {
    curl -s -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" "$@"
}

case "${1:-create}" in
--list)
    api https://api.github.com/user/codespaces | python3 -c "
import json,sys
d=json.load(sys.stdin)
for c in d.get('codespaces', []):
    print(c.get('name'), '|', c.get('state'), '|', c.get('web_url'))
"
    exit 0
    ;;
--delete)
    NAME="${2:-}"
    [ -n "$NAME" ] || { echo "falta o nome" >&2; exit 1; }
    api -X DELETE "https://api.github.com/user/codespaces/$NAME" -o /dev/null -w "apagado: %{http_code}\n"
    exit 0
    ;;
esac

say "criando o codespace em $REPO"
RESPONSE=$(api -X POST "https://api.github.com/repos/$REPO/codespaces" \
    -d "{\"ref\":\"main\",\"machine\":\"$MACHINE\",\"devcontainerPath\":\".devcontainer/devcontainer.json\",\"multiRepoPermissionsOptOut\":true,\"displayName\":\"cinestream-tv\",\"gitStatus\":{\"uncommittedChanges\":\"ignore\",\"untrackedFiles\":\"ignore\"}}")
NAME=$(printf '%s' "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('name') or '')")
[ -n "$NAME" ] || { echo "$RESPONSE" | head -5; exit 1; }
say "codespace: $NAME"

for i in $(seq 1 30); do
    STATE=$(api "https://api.github.com/user/codespaces/$NAME" | python3 -c "import json,sys; print(json.load(sys.stdin).get('state'))")
    say "estado: $STATE"
    [ "$STATE" = "Available" ] && break
    [ "$STATE" = "Shutdown" ] && { echo "o codespace parou; veja o log em cinestream-start.log no repositorio" >&2; exit 1; }
    sleep 20
done

URL="https://$NAME-$PORT.app.github.dev"
sleep 20
CODE=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$URL/healthz" || echo 000)

cat <<EOF

  endereco do gateway : $URL
  healthz             : http $CODE   (302 = a porta ainda esta privada)

Falta um clique: abra
  https://github.com/codespaces/$NAME
na aba Ports, ponha a porta $PORT em "Public". Depois disso o mesmo endereco
responde 200 e o player do site aponta para ele:

  python3 deploy/set_api.py --host $NAME-$PORT.app.github.dev

Para derrubar:
  ./codespace.sh --delete $NAME
EOF
