#!/usr/bin/env bash
# Stop the local development stack started by scripts/dev-up.sh. Data is kept; pass --wipe to
# delete the dev database, Redis and Caddy's local CA as well.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.dev"

for name in api worker; do
  if [ -f "$STATE/$name.pid" ]; then
    kill "$(cat "$STATE/$name.pid")" 2>/dev/null
    rm -f "$STATE/$name.pid"
  fi
done
# `uv run` may leave its child running: stop only our own processes, matched by their command line.
for pid in $(pgrep -f 'uvicorn khugjil.web.app:production_app' 2>/dev/null) \
           $(pgrep -f 'bin/khugjil worker' 2>/dev/null); do
  kill "$pid" 2>/dev/null
done

docker stop khugjil-dev-caddy khugjil-dev-redis khugjil-dev-postgres >/dev/null 2>&1
if [ "${1:-}" = "--wipe" ]; then
  docker rm -v khugjil-dev-caddy khugjil-dev-redis khugjil-dev-postgres >/dev/null 2>&1
  docker volume rm khugjil-dev-caddy-data >/dev/null 2>&1
  rm -rf "$STATE"
fi
echo "Stopped."
