#!/usr/bin/env bash
# Stop what scripts/public-up.sh started. Data and keys are kept. `--wipe` deletes the database,
# Redis and everything in .dev/public (keys included): only for starting over.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.dev/public"

# Each recorded process together with its child (`uv run` may leave the child running); never the
# local demo stack's processes, which run the same commands.
for name in tunnel api worker; do
  if [ -f "$STATE/$name.pid" ]; then
    pid="$(cat "$STATE/$name.pid")"
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
    rm -f "$STATE/$name.pid"
  fi
done
for pid in $(pgrep -f 'uvicorn khugjil.web.app:production_app --factory --host 127.0.0.1 --port 8100' 2>/dev/null); do
  kill "$pid" 2>/dev/null
done
docker stop khugjil-public-caddy khugjil-public-redis khugjil-public-postgres >/dev/null 2>&1

if [ "${1:-}" = "--wipe" ]; then
  docker rm khugjil-public-caddy khugjil-public-redis khugjil-public-postgres >/dev/null 2>&1
  docker volume rm khugjil-public-pgdata khugjil-public-redis >/dev/null 2>&1
  rm -rf "$STATE"
fi
echo "Stopped."
