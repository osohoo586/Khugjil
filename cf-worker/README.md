# Cloudflare Email Worker (port-25 fallback, optional)

Use this only if the server cannot receive mail on TCP port 25 (PLAN Q4, still open). Normally Gmail
delivers forwarded bank emails straight to the server's SMTP service at `in.khugjil.usukh.shop`.
With the fallback, Cloudflare Email Routing receives them instead and this Worker forwards each
raw message, unchanged and only in memory, to `POST https://app.khugjil.usukh.shop/hooks/cf`. The
server then runs exactly the same checks as for SMTP.

**Trade-off (threat model I10):** Cloudflare can read the forwarded emails on this path. The Worker
neither logs nor stores them (`observability` is off, and the code has no logging).

## Setup (only when needed)

1. Generate a shared secret on your own computer, for example `openssl rand -hex 32`. Put it into
   the server's env file as `KHJ_CF_INGEST_SECRET` and restart the `api` service. Don't paste it
   anywhere else.
2. Cloudflare dashboard → `usukh.shop` → **Email → Email Routing**: enable Email Routing for the
   subdomain `in.khugjil.usukh.shop`. Cloudflare then replaces the MX record of
   `in.khugjil.usukh.shop` (which pointed at the server) with its own mail servers. The exact menu
   names may differ.
3. Deploy the Worker and store the same secret in it:
   ```sh
   cd cf-worker
   npx wrangler@4.154.0 login
   npm run deploy
   npx wrangler@4.154.0 secret put KHJ_CF_INGEST_SECRET
   ```
4. Email Routing → **Routing rules**: send all addresses of `in.khugjil.usukh.shop` (catch-all) to
   the Worker `khugjil-email`.
5. Check the admin panel's **Имэйл холболт** page after the next bank email (or redo Gmail's
   forwarding confirmation; its code arrives through the same path).

To go back to direct SMTP, disable Email Routing for the subdomain and restore
`MX in.khugjil.usukh.shop → 10 in.khugjil.usukh.shop.`

## To verify with a real email (once Q1–Q3 are answered)

Cloudflare may add its own ARC headers before the Worker sees the message. The server trusts only
the top ARC set when Google sealed it, so on this path the bank's original DKIM signature has to
pass by itself. Check one real forwarded email on **Имэйл холболт** before relying on the fallback.

## Behaviour

| Hook answer | Worker |
|---|---|
| 202 (accepted or dropped by the checks) | done; dropped mail is not bounced, as with SMTP |
| 409 (same delivery already taken) | done |
| 413 / message over 256 KB | rejected |
| 401, 404 (secret or setting missing), 429, 5xx, network | throws, so Email Routing answers with a temporary failure and Gmail retries |

`npm test` runs the Worker tests with Node only (no dependencies). The signature is checked against
`shared/cf_hook_vectors.json`, which the server's tests use too.
