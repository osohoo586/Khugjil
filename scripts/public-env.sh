# Settings for the Mac-as-server stack, sourced by public-up.sh and check-email.sh.
# Reads the per-Mac files in .dev/public (site.sh, bank.sh, generated secrets); prints nothing secret.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.dev/public"
mkdir -p "$STATE"
chmod 700 "$STATE"

# Optional overrides kept on this Mac (e.g. KHJ_PUBLIC_INBOUND_HOST when Cloudflare Email Routing
# serves the zone apex instead of in.khugjil.usukh.shop).
if [ -f "$STATE/site.sh" ]; then
  # shellcheck source=/dev/null
  . "$STATE/site.sh"
fi
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
