# Shared settings for the local development stack (sourced by dev-up.sh and dev-seed.sh).
# Every value here is a throwaway development value, not a secret.
DEV_PG_PW="dev-only-not-secret"

export KHJ_ENV=development
export KHJ_DOMAIN=khugjil.localhost
export KHJ_PUBLIC_PORT=8443
export KHJ_DEV_ROUTES=true
export KHJ_AGENT_DEVICES_ENABLED=true
export KHJ_MASTER_KEY="ZGV2LW9ubHktbm90LXNlY3JldC1rZXktMzJieXRlcyE="   # "dev-only-not-secret-key-32bytes!"
export KHJ_DATABASE_URL="postgresql+psycopg://khugjil_app:${DEV_PG_PW}@127.0.0.1:55452/khugjil"
export KHJ_MIGRATION_DATABASE_URL="postgresql+psycopg://postgres:${DEV_PG_PW}@127.0.0.1:55452/khugjil"
export KHJ_REDIS_URL="redis://127.0.0.1:56399/0"
# Fake bank values (the real ones are PLAN Q1-Q3 and are never guessed). They match the test
# fixtures, so dev-seed.sh can sign demo bank emails with throwaway test keys.
export KHJ_BANK_SENDERS='["notify@bank.example"]'
export KHJ_BANK_DKIM_DOMAINS='["bank.example"]'
export KHJ_BANK_SUBJECT_RE='^Test income notification$'
