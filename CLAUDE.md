# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

`SPEC.md` is the authoritative spec for a multi-tenant **bank income notifier** (originally Khan Bank only); `PLAN.md` is the approved design (architecture, data model, threat model, open questions Q1–Q36). Phase 1 is implemented on the `phase1` branch. Phase 2 (Windows desktop agent, `PLAN-PHASE2.md`) is implemented on the `phase2` branch with the plan's default answers; its open questions P2-Q1…Q12 still stand, and the Windows-only behaviour needs the manual checklist in `docs/agent.md`. The production domain is `khugjil.usukh.shop` (Q7). The default layout is `app.`/`admin.`/`in.` subdomains, but `KHJ_APP_HOSTNAME`/`KHJ_ADMIN_HOSTNAME`/`KHJ_INBOUND_HOSTNAME` can override them; behind Cloudflare the names are `khugjil.usukh.shop` (admin) and `khugjil-kass.usukh.shop` (cashier).

**Banks.** One bank per deployment, chosen by `KHJ_BANK_PARSER`. `golomt_v1` (Golomt Bank "Easy Info") is built from the real layout and runs live on the Mac-server deployment. `khan_v1` is still built and tested only against fake values: the Khan Bank placeholders (Q1–Q3) and its real email layout (Q6, Q8, Q9, Q11) are unknown. This file summarizes the parts that are easiest to get wrong; when they disagree, `SPEC.md` wins.

## Commands

Run from the repo root unless noted. Server tests need the throwaway Postgres/Redis containers.

```sh
make services                  # start test Postgres (127.0.0.1:55442) + Redis (:56389)
make lint typecheck security   # ruff + format check, mypy --strict, bandit + pip-audit
make test                      # full pytest suite
make coverage                  # tests + per-package ≥95% gate (scripts/coverage_gate.py)
make check                     # lint + typecheck + security + coverage (the server CI job)
make cashier-check             # cashier: eslint, tsc (app + service worker), vitest, npm audit
make cf-worker-test            # Cloudflare Email Worker tests (node --test)
make e2e                       # Playwright screenshots (builds cashier, renders admin fixtures)
cd e2e && npm run update       # re-record screenshot baselines after an intended UI change
scripts/dev-up.sh              # local stack on https://{admin,app}.khugjil.localhost:8443; dev-seed.sh for demo data
make e2e-flows                 # real-browser flows against that running stack (invite, TOTP, pairing, SSE)
make sync-assets               # after editing shared/tokens.css or shared/fonts
make agent-check               # desktop agent: capability guard, rustfmt, clippy, cargo test (Rust on PATH)
cd server && uv run pytest tests/unit/test_foundation.py::test_tokens   # single test
cd server && uv run pytest -m "invariant"                                # SPEC §3 invariant tests only
cd cashier && npx vitest run src/format.test.ts                         # single cashier test file
cd server && uv run khugjil {keys generate|migrate|smtp|worker|telegram set-webhook|tenant create --name N}
```

**Mac-server deployment (real bank data).** This Mac serves the live site until a VPS exists. `docs/mac-server.md` is the Mongolian guide.
- `scripts/public-up.sh` starts the stack and `scripts/public-down.sh` stops it. The path is Cloudflare Tunnel → Caddy on 127.0.0.1:8180, and inbound mail goes Cloudflare Email Routing → `cf-worker` → `/hooks/cf`.
- This stack is separate from the `dev-up.sh` demo: it has its own containers and volumes and runs with production settings.
- Keys and per-site settings (`bank.sh`, `site.sh`) live in the gitignored `.dev/public/`. Never print them.
- `scripts/check-email.sh <file.eml> [--deliver]` runs a saved bank email through the real checks. It prints only outcomes and structure, never content.
- Before a long shutdown, deploy the Worker with `--var KHJ_PAUSED:1` (accept and discard). Otherwise Gmail keeps retrying and sends the owner delivery-failure notices.
- `public-up.sh` runs api, worker and the tunnel under a restart loop (`supervise`). The pid file holds the loop, and `public-down.sh` stops the loop before its child. It also starts `caffeinate -i -s`. Nothing restarts the stack after a reboot.

- Python deps are managed with `uv` (`server/uv.lock`); never pip-install into the project. The cashier pins exact npm versions and overrides `minimatch` to a patched release.
- pytest runs with sockets disabled except localhost (pytest-socket); DKIM DNS goes through a stub in tests. Tests use `tests/fixtures/world.py` (a migrated DB, app role under RLS, per-test tenant "worlds") and `tests/fixtures/api.py` (in-process HTTP harness for both hosts).
- Every SPEC §3 invariant has tests in `server/tests/invariants/`; none are xfail any more. New features touching an invariant must extend those tests.
- Shared amount/time formatting is pinned by `shared/format_vectors.json`, used by both pytest and vitest.
- Writing `.env*` files is blocked by the user's permission settings, and a hook blocks Bash commands that mention `.env` (write such text with the Write tool). Configuration variables are documented in `docs/configuration.md`; the user creates the env file themselves and never pastes secrets or real bank emails into chat.

## Code map

- `server/src/khugjil/ingest/`: `checks.evaluate()` is the pure b–g pipeline; `pipeline.ingest()` adds check a and persistence; `smtp.py` and `api_v1/hooks.py` are the two transports. `forwarding.py` recognises Gmail forwarding-confirmation mails and shows the code or link to the owner.
- `server/src/khugjil/ingest/parsers/` has one versioned module per bank layout. `checks.py` maps the parser version to an adapter that turns the parse result into a common `Income`. Golomt differs from Khan in these ways, and the checks handle each one:
  - The credited account is masked (`123*****89`). `masked_match` matches it digit by digit to exactly one registered full number.
  - There is no journal number, so the idempotency material is the bank's own DKIM signature (`dkim:<sig>`).
  - There is no debit side, so the own-transfer and Дт = Кт checks cannot run.
  - Expense emails raise `NotIncomeError`.
  - Bank-logo image parts are skipped by file name.
- Tenant feature flags are columns on `tenant`, set on the admin Settings page.
  - `auto_add_accounts` (migration 0003) registers an unknown credited account from a bank-signed email.
  - `cashier_pin_shifts` (0004) can be turned off. The DB default is true, so test worlds keep PINs; existing tenants and CLI-created ones get false. With it off, `GET /api/v1/device` opens a shift automatically for the first owner, then admin, who shares a group with the device and has web consent.
- `server/src/khugjil/db/`: models + migration `0001` (authoritative DDL, RLS policies, SECURITY DEFINER lookups); later migrations are numbered files in `db/migrations/versions/` written as raw SQL with `op.execute`. All tenant data access goes through `tenancy.tenant_session()`; cross-tenant lookups only through `tenancy.Resolver`.
- `server/src/khugjil/routing/eligibility.py` (pure) + `delivery/service.py` (fanout/send with re-check) + `delivery/channels.py` (SSE, Web Push, Telegram).
- `server/src/khugjil/web/app.py` dispatches by Host to the cashier API (`api_v1/`) and the admin panel (`admin/`).
- `cashier/src/` is framework-free TypeScript; `dom.h()` is the only way to build elements (ESLint bans innerHTML). `CashierApp` takes a `Platform` (`platform-web.ts` for the browser, `platform-agent.ts` for the Tauri agent); `vite.agent.config.ts` builds `agent.html` + `overlay.html` into `dist-agent/`.
- `agent/src-tauri/` is the Tauri v2 agent. The webview never sees the device token: `routes.rs` allowlists API calls, `api.rs` adds the bearer token, `stream.rs` holds SSE. Capabilities are per window and guarded by `agent/scripts/check-capabilities.mjs`. Server side: bearer auth only for `kind = agent` devices, behind `KHJ_AGENT_DEVICES_ENABLED` (tests in `server/tests/invariants/test_inv09_agent_devices.py`).

## Required workflow (SPEC §12)

1. **First deliverable is `PLAN.md`, then STOP for approval.** It must contain: architecture, data model, STRIDE threat model, ASCII wireframes of the cashier view (full and compact) and the main admin pages, and open questions. Write no implementation code until the user approves the plan.
2. After approval, write the security-invariant tests **first**. Then implement Phase 1 in small commits, in this order: server core → admin panel → cashier view → Telegram.
3. **Phase 2 (Tauri desktop agent)** is planned in `PLAN-PHASE2.md` and built on the `phase2` branch. The cashier UI stays a standalone bundle that talks to the server only through the versioned JSON API plus SSE; the agent swaps only the platform adapters.
4. **Never invent `[PLACEHOLDER]` values.** These include each bank's sender address, subject pattern and DKIM `d=` domain, the interbank-incoming format, whether port 25 is blocked, and the retention period. Ask the user.
5. Never put real account numbers, names or emails in code, tests, fixtures or commits. Use only fake data, like the sample in SPEC §4.
6. A `README.md` is required, with parts in Mongolian: the Gmail filter guide and cashier PC setup, including the Windows 11 full-screen notification limitation.

## Stack (SPEC §11)

- **Server:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, PostgreSQL, Redis + arq (background jobs), aiosmtpd (inbound SMTP on :25), pywebpush (VAPID), pydantic-settings, structlog with a redaction filter, libsodium field encryption.
- **Admin panel:** server-rendered Jinja + HTMX.
- **Cashier view:** a standalone TypeScript + Vite static bundle served by Caddy, with minimal dependencies and no UI kit. It must stay decoupled from server rendering so a Tauri shell can load it unchanged later.
- **Ops:** Docker Compose, Caddy for TLS, non-root containers with read-only filesystems, only ports 25 and 443 exposed.
- **CI gates:** `ruff`, `mypy --strict`, `bandit`, `pip-audit`, `npm audit`, `pytest`, Playwright screenshot tests. Coverage must be **≥95%** on the parser, filters, routing, consent, on-duty logic and tenant isolation.

## Architecture: the email pipeline

Data flows like this: tenant's Gmail forwarding filter → per-tenant secret inbound address (`<random 16+ chars>@in.<DOMAIN>`) → aiosmtpd. If port 25 is blocked, the fallback path is Cloudflare Email Routing → a Worker → an HMAC-signed, timestamped HTTPS POST into the **same** pipeline. The pipeline then runs: validation chain → bank parser (`khan_v1` or `golomt_v1`) → routing → Notifier fan-out.

**Validation runs in this fixed order. Any failure drops the email.** The only thing logged is `tenant_id`, `reason_code` and `sha256(Message-ID)`.
1. The envelope RCPT is an active tenant inbound address.
2. The original From is on the allowlist.
3. DKIM passes for the bank's domain, OR an ARC chain from google.com verifies and records dkim=pass.
4. There are no attachments and the size is ≤256 KB.
5. The subject and body match the strict template.
6. The Кт/To account belongs to the tenant AND the Дт/From account does not (own transfers are skipped).
7. The Дт amount equals the Кт amount, in the same currency, and is > 0.

The deny-list (OTP, код, нууц үг, …) is checked on the subject and the body, **excluding** the payer-written description.

**Parser (`khan_v1`, versioned):** first normalize the HTML table to text by cell and row. Then parse anchored sections in a fixed order. Treat Latin `K` and Cyrillic `К` in "Кт" as the same. Capture the description **last**, strictly between its label and "Харилцагч танд баярлалаа", so injected label-like text can't affect other fields. Use `Decimal` for amounts and the `Asia/Ulaanbaatar` timezone. If the sender and DKIM are valid but the template doesn't match, alert the owner with no email content. Fuzz the parser with hypothesis. `golomt_v1` follows the same rules: it captures the description last, between its label and the balance label, and it never reads the balance.

**Delivery gate:** a transaction is delivered only when ALL of these hold: the account is active, the group is active, the device or member is paired or verified, the member has active versioned consent, and the member is on duty. Off-duty means nothing is sent and **nothing is queued**. The idempotency key is `HMAC(tenant_id + journal_no + system_date + to_account)`, giving exactly-once delivery per device and channel.

**Channels** sit behind a `Notifier` interface, so Viber or SMS can be added later without implementing them now:
- **Cashier PWA (primary):** SSE with a 15s heartbeat and backoff reconnect, refetching items missed during the shift. In-page toast, chime, Web Push when the page is unfocused, and title flashing. Pairing uses one-time codes that are ≥128-bit, single use and valid 10 min, and binds a device credential to an httpOnly SameSite=Strict cookie. Revoking a device closes its SSE stream. Shifts start and end with a member PIN (argon2id), unless the tenant has turned `cashier_pin_shifts` off.
- **Telegram (optional, owners):** webhook with `secret_token`, **no `parse_mode`**, link previews off, and `/stop` revokes consent.

## Hard security invariants (SPEC §3): each needs an automated test

- **No mailbox access, ever.** No IMAP, POP3, Gmail API, Microsoft Graph or mail OAuth, not even as an optional feature.
- **Never extract, store, show or send** the balance, payer name or payer account. Persist only: amount, currency, txn datetime, description, the `to_account` HMAC and the `journal_no` HMAC. Account numbers are encrypted at rest with an HMAC for lookup.
- **Never store or log the raw email.** Process it in memory only.
- **The description is untrusted payer input.** Plain text everywhere, escaped in the UI, with control, zero-width and bidi characters stripped, truncated to 200 chars.
- No LLM in the processing path. No link following.
- Strict tenant isolation, proven by tests.
- Consent revocation takes effect immediately.
- Web: argon2id passwords, mandatory TOTP for owners and admins, CSRF protection, strict CSP with no inline scripts, HSTS, **no third-party JS or CDN**, self-hosted fonts.

## Security review on every change

The owner wants a security verdict for every code change, whoever made it. A local Stop hook enforces this, and it is not committed:
- `.claude/settings.local.json` points to `~/.claude/hooks/khugjil-security-gate.py`.
- The state is kept in `.git/khugjil-security-reviewed.json`.
- When files changed since the last review, the hook blocks one stop and lists them.

Review only those changes. Give the owner each finding with its severity, CVSS 3.1 score, CWE, `file:line` and fix, or say plainly that nothing dangerous was found. Use OWASP ASVS 5.0 Level 2 and the SPEC §3 invariants as the baseline. Full assessments are dated files in the gitignored `.dev/security/`; read the latest before reviewing, and never commit or publish them.

What to check in this codebase:
- **Ingest (`ingest/`):**
  - Checks a–g keep their order and strictness.
  - A bank-signed email is evidence only once and only while fresh, so think about replay.
  - Anything that widens which accounts count as the tenant's (masked matching, `auto_add_accounts`) is high risk.
- **Authentication:**
  - Every secret or factor check (password, TOTP, PIN, pairing or invite code) needs a failure counter with lockout or back-off, not only a time-window rate limit.
  - Tokens must come from `crypto.new_token`.
- **Rate limits:** Rate limits keyed on `request.client.host` must work behind Caddy and Cloudflare. Check what the proxy actually forwards.
- **Authorization:**
  - New admin routes use `Owner`/`Manager`/`Reader`.
  - Settings that weaken a security control are owner-only, need `require_stepup` and are audited with old and new values.
  - New cashier routes take `Device_` and filter with `visible_account_ids`.
- **Tenancy:** Data access goes only through `tenant_session()`. Raw SQL and new `Resolver` lookups return ids only. A new table needs RLS `ENABLE` + `FORCE` and a policy in its migration.
- **Output:** The cashier builds elements only with `dom.h()`, the admin uses Jinja autoescape with no `|safe`, and CSP and Trusted Types stay intact.
- **Agent:**
  - Each new command or route is added to `routes.rs` and the capability files and passes `check-capabilities.mjs`.
  - The device token never reaches the webview.
- **Secrets and data:** Never log raw email, account numbers, tokens or chat ids. Never add real bank data to fixtures.
- **Ops scripts and deploy:**
  - Keep least privilege: no superuser credentials in the app's environment, a Redis password, and only loopback binds.
- **Dependencies:** A changed lock file means running `pip-audit`, `npm audit` or `cargo audit`. A new dependency needs a reason.

## UI constraints (SPEC §7–8)

- The notification text is identical on every channel and has no emoji. Amounts are formatted `1,234,567 ₮` with decimals only when non-zero, and other currencies show their ISO code.
- The visual style is calm and ledger-like and must not look AI-generated: no emoji, gradients, glassmorphism, glow or illustrations. Use 1px borders instead of shadows, a radius of 6px or less, an 8px grid, IBM Plex Sans (self-hosted), tabular numerals, and right-aligned amounts. There is one accent color, chosen per tenant: #1F5E3B or #1D3557.
- Microcopy is formal Mongolian ("Та") with no exclamation marks.
- The cashier view shows **only** what SPEC §8.3 lists: no navigation, no settings, no more than 10 items of history, and no export.
- Design tokens live in one CSS-variables file shared by the admin panel and the cashier view. `/dev/ui` is the component inventory page and is disabled in production.
