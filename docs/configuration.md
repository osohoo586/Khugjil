# Configuration

All settings are environment variables with the `KHJ_` prefix (see `server/src/khugjil/settings.py`).
Put them in the env file that you create yourself next to `deploy/docker-compose.yml`. Never commit
that file. Generate key material locally with `cd server && uv run khugjil keys generate`.

| Variable | Required | Notes |
|---|---|---|
| `KHJ_ENV` | no | `production` (default), `development` or `test` |
| `KHJ_DOMAIN` | yes | Base domain; `app.`, `admin.` and `in.` subdomains are derived from it |
| `KHJ_MASTER_KEY` | yes | Base64 of exactly 32 random bytes; all field-encryption and HMAC keys derive from it |
| `KHJ_DATABASE_URL` | yes | `postgresql+psycopg://khugjil_app:…@postgres:5432/khugjil` (non-owner role) |
| `KHJ_REDIS_URL` | yes | `redis://redis:6379/0` |
| `KHJ_BANK_SENDERS` | yes | JSON array of Khan Bank From addresses (PLAN Q1) |
| `KHJ_BANK_DKIM_DOMAINS` | yes | JSON array of DKIM `d=` domains (PLAN Q2) |
| `KHJ_BANK_SUBJECT_RE` | yes | Python regex for the incoming-transfer subject (PLAN Q3) |
| `KHJ_RETENTION_DAYS` | no | Transaction retention, default 90 (PLAN Q5) |
| `KHJ_SMTP_TLS_CERT`, `KHJ_SMTP_TLS_KEY` | prod | Certificate for `in.<DOMAIN>` (read-only from Caddy's storage) |
| `KHJ_TELEGRAM_BOT_TOKEN`, `KHJ_TELEGRAM_WEBHOOK_SECRET`, `KHJ_TELEGRAM_BOT_USERNAME` | optional | Telegram channel |
| `KHJ_VAPID_PRIVATE_KEY`, `KHJ_VAPID_PUBLIC_KEY`, `KHJ_VAPID_SUBJECT` | yes for Web Push | `khugjil keys generate` prints a pair |
| `KHJ_CF_INGEST_SECRET` | only for the Cloudflare fallback | PLAN Q4 |

The three bank values have no defaults: the app refuses to start until they are set.
