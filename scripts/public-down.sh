#!/usr/bin/env bash
# Stop what scripts/public-up.sh started. Data and keys are kept. `--wipe` deletes the database,
# Redis and everything in .dev/public (keys included): only for starting over.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.dev/public"

# Each recorded restart loop, then what it was running (and that process's own children, since
# `uv run` may leave its child running); never the local demo stack's processes, which run the
# same commands. The loop goes first so it cannot start the process again.
for name in tunnel api worker caffeinate; do
  if [ -f "$STATE/$name.pid" ]; then
    pid="$(cat "$STATE/$name.pid")"
    kids="$(pgrep -P "$pid" 2>/dev/null)"
    kill "$pid" 2>/dev/null
    for kid in $kids; do
      pkill -P "$kid" 2>/dev/null
      kill "$kid" 2>/dev/null
    done
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
