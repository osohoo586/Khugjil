#!/usr/bin/env bash
# Stop the local development stack started by scripts/dev-up.sh. Data is kept; pass --wipe to
# delete the dev database, Redis and Caddy's local CA as well.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.dev"

# Stop each recorded `uv run` process together with its child (uv may leave the child running).
# Only processes this stack started: the Mac-server stack (scripts/public-up.sh) runs the same
# commands and must keep running.
for name in api worker; do
  if [ -f "$STATE/$name.pid" ]; then
    pid="$(cat "$STATE/$name.pid")"
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
    rm -f "$STATE/$name.pid"
  fi
done
for pid in $(pgrep -f 'uvicorn khugjil.web.app:production_app --factory --host 127.0.0.1 --port 8000' 2>/dev/null); do
  kill "$pid" 2>/dev/null
done

docker stop khugjil-dev-caddy khugjil-dev-redis khugjil-dev-postgres >/dev/null 2>&1
if [ "${1:-}" = "--wipe" ]; then
  docker rm -v khugjil-dev-caddy khugjil-dev-redis khugjil-dev-postgres >/dev/null 2>&1
  docker volume rm khugjil-dev-caddy-data >/dev/null 2>&1
  rm -rf "$STATE"
fi
echo "Stopped."
