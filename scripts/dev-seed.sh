#!/usr/bin/env bash
# Demo data for the local stack started by scripts/dev-up.sh (fake values only).
#   scripts/dev-seed.sh                      accounts, groups, two cashiers, today's and yesterday's history
#   scripts/dev-seed.sh txn [AMOUNT] [TEXT]  one new transaction now (watch it arrive on the cashier screen)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=dev-config.sh
. "$ROOT/scripts/dev-config.sh"
cd "$ROOT/server"
exec uv run python -m tests.fixtures.dev_seed "$@"
