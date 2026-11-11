#!/usr/bin/env bash
# Run the system on THIS Mac and publish it through a Cloudflare Tunnel (until a real server):
#   admin panel   https://khugjil.usukh.shop
#   cashier view  https://khugjil-kass.usukh.shop
#   bank email    <secret>@in.khugjil.usukh.shop  (Cloudflare Email Routing -> cf-worker -> tunnel)
#
# Separate from the localhost demo stack (scripts/dev-up.sh): its own containers and database, and
# keys generated on this Mac into .dev/public/ (never committed, never printed). Real bank data
# arrives here, so it runs with production settings (no dev pages). Nothing listens on a public
# interface: the tunnel connects outwards and reaches Caddy on 127.0.0.1:8180 only.
# Stop with scripts/public-down.sh. Guide: docs/mac-server.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.dev/public"
mkdir -p "$STATE"
chmod 700 "$STATE"

ADMIN_HOST="${KHJ_PUBLIC_ADMIN_HOST:-khugjil.usukh.shop}"
APP_HOST="${KHJ_PUBLIC_APP_HOST:-khugjil-kass.usukh.shop}"
IN_HOST="${KHJ_PUBLIC_INBOUND_HOST:-in.khugjil.usukh.shop}"
TUNNEL="${KHJ_TUNNEL_NAME:-khugjil}"

# -- bank settings: from Gmail "Show original" of a real notification; never guessed ------------
if [ ! -f "$STATE/bank.sh" ]; then
  cat <<EOF
Missing $STATE/bank.sh. Create it with the bank's real values (Gmail -> the bank email -> ⋮ ->
"Show original"; nothing here is secret), for example:

  export KHJ_BANK_PARSER=golomt_v1
  export KHJ_BANK_SENDERS='["<From address>"]'
  export KHJ_BANK_DKIM_DOMAINS='["<domain from: DKIM: PASS with domain …>"]'
  export KHJ_BANK_SUBJECT_RE='^<exact subject, digits replaced by \\d+>$'
EOF
  exit 1
fi
# shellcheck source=/dev/null
. "$STATE/bank.sh"

# -- secrets: generated once on this Mac, readable only by you -----------------------------------
secret() {  # file, generator command
  local f="$STATE/$1"
  if [ ! -s "$f" ]; then (umask 077 && eval "$2" >"$f"); fi
  cat "$f"
}
PG_PW="$(secret pg-password 'openssl rand -hex 24')"
KHJ_MASTER_KEY="$(secret master-key 'openssl rand -base64 32')"
KHJ_CF_INGEST_SECRET="$(secret cf-ingest-secret 'openssl rand -hex 32')"
export KHJ_MASTER_KEY KHJ_CF_INGEST_SECRET
if [ ! -s "$STATE/vapid" ]; then
  (umask 077 && cd "$ROOT/server" && uv run khugjil keys generate | grep '^KHJ_VAPID_' >"$STATE/vapid")
fi
set -a
# shellcheck source=/dev/null
. "$STATE/vapid"
set +a

export KHJ_ENV=production
export KHJ_DOMAIN="${ADMIN_HOST}"
export KHJ_APP_HOSTNAME="$APP_HOST" KHJ_ADMIN_HOSTNAME="$ADMIN_HOST" KHJ_INBOUND_HOSTNAME="$IN_HOST"
export KHJ_VAPID_SUBJECT="https://$ADMIN_HOST"
export KHJ_DATABASE_URL="postgresql+psycopg://khugjil_app:${PG_PW}@127.0.0.1:55462/khugjil"
export KHJ_MIGRATION_DATABASE_URL="postgresql+psycopg://postgres:${PG_PW}@127.0.0.1:55462/khugjil"
export KHJ_REDIS_URL="redis://127.0.0.1:56409/0"

container() {  # name, image, extra docker run args...
  local name=$1 image=$2; shift 2
  if [ -z "$(docker ps -aq -f "name=^${name}$")" ]; then
    docker run -d --name "$name" --restart unless-stopped "$@" "$image" >/dev/null
  elif [ -z "$(docker ps -q -f "name=^${name}$")" ]; then
    docker start "$name" >/dev/null
  fi
}

echo "· database and redis (own volumes)"
container khugjil-public-postgres postgres:17-alpine -p 127.0.0.1:55462:5432 \
  -e POSTGRES_PASSWORD="$PG_PW" -e POSTGRES_DB=khugjil -v khugjil-public-pgdata:/var/lib/postgresql/data
container khugjil-public-redis redis:7-alpine -p 127.0.0.1:56409:6379 -v khugjil-public-redis:/data
until docker exec khugjil-public-postgres pg_isready -U postgres -d khugjil >/dev/null 2>&1; do sleep 1; done
docker exec -i khugjil-public-postgres psql -U postgres -d khugjil -q -v pw="$PG_PW" >/dev/null <<'SQL'
SELECT format('CREATE ROLE khugjil_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS', :'pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'khugjil_app') \gexec
SQL

echo "· migrations"
(cd "$ROOT/server" && uv run khugjil migrate >/dev/null)

echo "· cashier bundle"
(cd "$ROOT/cashier" && npm run build >/dev/null)

start() {  # name, command...
  local name=$1; shift
  if [ -f "$STATE/$name.pid" ] && kill -0 "$(cat "$STATE/$name.pid")" 2>/dev/null; then return; fi
  (cd "$ROOT/server" && { nohup "$@" </dev/null >"$STATE/$name.log" 2>&1 & echo $! >"$STATE/$name.pid"; })
}
echo "· api and worker"
start api uv run uvicorn khugjil.web.app:production_app --factory --host 127.0.0.1 --port 8100
start worker uv run khugjil worker

echo "· caddy (http on 127.0.0.1:8180, behind the tunnel)"
container khugjil-public-caddy caddy:2-alpine -p 127.0.0.1:8180:8180 \
  -e KHJ_APP_HOSTNAME="$APP_HOST" -e KHJ_ADMIN_HOSTNAME="$ADMIN_HOST" \
  -v "$ROOT/deploy/Caddyfile.public:/etc/caddy/Caddyfile:ro" \
  -v "$ROOT/cashier/dist:/srv/cashier:ro" \
  --add-host host.docker.internal:host-gateway
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $ADMIN_HOST" http://127.0.0.1:8180/login || true)
  [ "$code" = 200 ] && break
  sleep 1
done
[ "$code" = 200 ] || { echo "admin panel did not come up (see $STATE/api.log)"; exit 1; }

if [ ! -f "$STATE/tenant.txt" ]; then
  echo "· your shop"
  (cd "$ROOT/server" && uv run khugjil tenant create --name "${KHJ_SHOP_NAME:-Миний дэлгүүр}") \
    | grep -E '^(tenant_id|inbound_address|owner_invite)=' >"$STATE/tenant.txt"
  chmod 600 "$STATE/tenant.txt"
fi

if [ -n "${KHJ_PUBLIC_NO_TUNNEL:-}" ]; then
  echo "· tunnel skipped (KHJ_PUBLIC_NO_TUNNEL): reachable only here, e.g."
  echo "  curl -H 'Host: $ADMIN_HOST' http://127.0.0.1:8180/login"
  exit 0
fi

echo "· cloudflare tunnel"
command -v cloudflared >/dev/null || { echo "Install it first: brew install cloudflared"; exit 1; }
[ -f "$HOME/.cloudflared/cert.pem" ] || {
  echo "Sign in to Cloudflare once (a browser opens; choose usukh.shop): cloudflared tunnel login"
  exit 1
}
tunnel_id() {
  cloudflared tunnel list -o json 2>/dev/null |
    python3 -c 'import json,sys; t=[x["id"] for x in json.load(sys.stdin) if x["name"]==sys.argv[1]]; print(t[0] if t else "")' "$TUNNEL"
}
TID="$(tunnel_id)"
if [ -z "$TID" ]; then
  cloudflared tunnel create "$TUNNEL" >/dev/null
  TID="$(tunnel_id)"
fi
for host in "$ADMIN_HOST" "$APP_HOST"; do
  cloudflared tunnel route dns "$TUNNEL" "$host" >/dev/null 2>&1 || true  # exists already: fine
done
cat >"$STATE/cloudflared.yml" <<EOF
tunnel: $TID
credentials-file: $HOME/.cloudflared/$TID.json
ingress:
  - hostname: $ADMIN_HOST
    service: http://127.0.0.1:8180
  - hostname: $APP_HOST
    service: http://127.0.0.1:8180
  - service: http_status:404
EOF
if ! { [ -f "$STATE/tunnel.pid" ] && kill -0 "$(cat "$STATE/tunnel.pid")" 2>/dev/null; }; then
  nohup cloudflared tunnel --no-autoupdate --config "$STATE/cloudflared.yml" run "$TUNNEL" \
    </dev/null >"$STATE/tunnel.log" 2>&1 &
  echo $! >"$STATE/tunnel.pid"
fi

invite="$(grep '^owner_invite=' "$STATE/tenant.txt" | cut -d= -f2-)"
inbound="$(grep '^inbound_address=' "$STATE/tenant.txt" | cut -d= -f2-)"
cat <<EOF

Up (this Mac is the server while this runs and the Mac is awake).
  Admin panel:     https://$ADMIN_HOST/login
  Cashier view:    https://$APP_HOST/
  First sign-in:   $invite
  Bank email to:   $inbound   (needs Cloudflare Email Routing + the Worker, docs/mac-server.md)

Logs: $STATE/*.log   Stop: scripts/public-down.sh
EOF
