#!/usr/bin/env bash
# Local development stack (not for production):
#   - Postgres 127.0.0.1:55452 and Redis 127.0.0.1:56399 in containers (separate from the test ones)
#   - migrations, then the API (uvicorn 127.0.0.1:8000) and the worker, in the background
#   - Caddy with a local CA: https://admin.khugjil.localhost:8443 and https://app.khugjil.localhost:8443
#   - on first run, a demo tenant; its owner invite link is printed
# Settings come from scripts/dev-config.sh (throwaway values). Logs and PIDs go to .dev/.
# Stop with scripts/dev-down.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.dev"
mkdir -p "$STATE"
# shellcheck source=dev-config.sh
. "$ROOT/scripts/dev-config.sh"
PG_PW="$DEV_PG_PW"

container() {  # name, image, port mapping, extra args...
  local name=$1 image=$2 ports=$3; shift 3
  if [ -z "$(docker ps -aq -f "name=^${name}$")" ]; then
    docker run -d --name "$name" -p "$ports" "$@" "$image" >/dev/null
  elif [ -z "$(docker ps -q -f "name=^${name}$")" ]; then
    docker start "$name" >/dev/null
  fi
}

echo "· database and redis"
container khugjil-dev-postgres postgres:17-alpine 127.0.0.1:55452:5432 \
  -e POSTGRES_PASSWORD="$PG_PW" -e POSTGRES_DB=khugjil
container khugjil-dev-redis redis:7-alpine 127.0.0.1:56399:6379
until docker exec khugjil-dev-postgres pg_isready -U postgres -d khugjil >/dev/null 2>&1; do sleep 1; done
docker exec khugjil-dev-postgres psql -U postgres -d khugjil -qtc \
  "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'khugjil_app') THEN
     CREATE ROLE khugjil_app LOGIN PASSWORD '${PG_PW}' NOSUPERUSER NOBYPASSRLS; END IF; END \$\$" >/dev/null

echo "· migrations"
(cd "$ROOT/server" && uv run khugjil migrate)

echo "· cashier bundle"
(cd "$ROOT/cashier" && npm run build >/dev/null)

start() {  # name, command...
  local name=$1; shift
  if [ -f "$STATE/$name.pid" ] && kill -0 "$(cat "$STATE/$name.pid")" 2>/dev/null; then return; fi
  # Background only the command itself (not a subshell), and detach it from this script's output.
  (cd "$ROOT/server" && { nohup "$@" </dev/null >"$STATE/$name.log" 2>&1 & echo $! >"$STATE/$name.pid"; })
}

echo "· api and worker"
start api uv run uvicorn khugjil.web.app:production_app --factory --host 127.0.0.1 --port 8000
start worker uv run khugjil worker

echo "· caddy (local CA)"
caddy_args=(-e KHJ_DOMAIN="$KHJ_DOMAIN"
  -v "$ROOT/deploy/Caddyfile.dev:/etc/caddy/Caddyfile:ro"
  -v "$ROOT/cashier/dist:/srv/cashier:ro"
  -v khugjil-dev-caddy-data:/data)
if [ "$(uname -s)" = Linux ]; then
  # Linux (CI): share the host network so Caddy reaches the API on 127.0.0.1 and listens on :8443.
  if [ -z "$(docker ps -aq -f name=^khugjil-dev-caddy$)" ]; then
    docker run -d --name khugjil-dev-caddy --network host -e KHJ_DEV_UPSTREAM=127.0.0.1:8000 \
      "${caddy_args[@]}" caddy:2-alpine >/dev/null
  else
    docker start khugjil-dev-caddy >/dev/null
  fi
else
  # Docker Desktop (macOS/Windows) forwards host.docker.internal to the host's loopback.
  container khugjil-dev-caddy caddy:2-alpine 127.0.0.1:8443:8443 "${caddy_args[@]}" \
    --add-host host.docker.internal:host-gateway
fi

for _ in $(seq 1 30); do
  code=$(curl -sk -o /dev/null -w '%{http_code}' --resolve admin.khugjil.localhost:8443:127.0.0.1 \
    https://admin.khugjil.localhost:8443/login || true)
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || { echo "admin panel did not come up (see $STATE/api.log)"; exit 1; }

if [ ! -f "$STATE/tenant.txt" ]; then
  echo "· demo tenant"
  (cd "$ROOT/server" && uv run khugjil tenant create --name "Жишээ дэлгүүр") >"$STATE/tenant.txt"
fi

cat <<EOF

Up.
  Admin panel:   https://admin.khugjil.localhost:8443/login
  Cashier view:  https://app.khugjil.localhost:8443/
  Owner invite (first sign-in; valid 72 h, single use):
    $(grep '^owner_invite=' "$STATE/tenant.txt" | cut -d= -f2-)

The certificate comes from Caddy's local CA, so the browser warns once per host (Advanced -> Proceed).
Demo data (accounts, cashiers, transactions): scripts/dev-seed.sh
Logs: $STATE/*.log   Stop: scripts/dev-down.sh
EOF
