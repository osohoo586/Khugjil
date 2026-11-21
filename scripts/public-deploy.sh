#!/usr/bin/env bash
# Deploy one commit to the live stack on this Mac (default: HEAD of this checkout).
#   scripts/public-deploy.sh [git-ref]
#
# The site runs from its own git worktree pinned to that commit (.dev/releases/<commit>, with
# .dev/release pointing at the one in use), never from this checkout. So editing templates,
# building the cashier, switching branches or uncommitted work here never reach the running site,
# and the restart loops in public-up.sh always reload the commit the database was migrated for.
#
# Order: prepare the new release while the old one keeps serving -> stop -> migrate -> start.
# Roll back: scripts/public-deploy.sh <earlier commit> (the last few releases are kept; our
# migrations only add columns and tables, so older code keeps working on the newer schema).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REF="${1:-HEAD}"
SHA="$(git -C "$ROOT" rev-parse --verify --quiet "$REF^{commit}")" || { echo "Unknown commit: $REF"; exit 1; }
SHORT="${SHA:0:12}"
RELEASES="$ROOT/.dev/releases"
DIR="$RELEASES/$SHORT"
KEEP=3

if [ "$REF" = HEAD ] && [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "Note: uncommitted changes in this checkout are NOT deployed; only commit $SHORT is."
fi

echo "· preparing release $SHORT (the site keeps running)"
mkdir -p "$RELEASES"
if [ ! -d "$DIR" ]; then
  git -C "$ROOT" worktree add --detach "$DIR" "$SHA" >/dev/null 2>&1
fi
(cd "$DIR/server" && uv sync --frozen >/dev/null)
(cd "$DIR/cashier" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)

echo "· switching"
"$ROOT/scripts/public-down.sh" >/dev/null
ln -sfn "releases/$SHORT" "$ROOT/.dev/release"
"$ROOT/scripts/public-up.sh"

# Keep the newest few releases besides the current one, for a quick roll-back.
current="$(cd "$ROOT/.dev/release" && pwd -P)"
ls -1t "$RELEASES" | while read -r name; do
  [ "$RELEASES/$name" = "$current" ] && continue
  echo "$name"
done | tail -n +"$KEEP" | while read -r old; do
  git -C "$ROOT" worktree remove --force "$RELEASES/$old" >/dev/null 2>&1 || true
done
git -C "$ROOT" worktree prune
