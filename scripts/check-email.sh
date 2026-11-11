#!/usr/bin/env bash
# Check a saved bank email (.eml) against the real rules without a test transfer:
#   scripts/check-email.sh ~/Downloads/original_msg.eml            dry run, nothing saved
#   scripts/check-email.sh ~/Downloads/original_msg.eml --deliver  ingest it for real
# Prints only outcomes, structure and amount; never the email's text. Delete the file afterwards.
set -euo pipefail
# shellcheck source=public-env.sh
. "$(cd "$(dirname "$0")" && pwd)/public-env.sh"
cd "$ROOT/server"
exec uv run python -m tests.fixtures.check_email "$@"
