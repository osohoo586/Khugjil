# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

`SPEC.md` is the authoritative spec for a multi-tenant **Khan Bank income notifier**; `PLAN.md` is the approved design (architecture, data model, threat model, open questions Q1–Q36). Phase 1 is implemented on the `phase1` branch. Phase 2 (Windows desktop agent, `PLAN-PHASE2.md`) is implemented on the `phase2` branch with the plan's default answers; its open questions P2-Q1…Q12 still stand, and the Windows-only behaviour needs the manual checklist in `docs/agent.md`. The production domain is `khugjil.usukh.shop` (Q7; `app.`/`admin.`/`in.` subdomains, Cloudflare DNS with records set to DNS only). The Khan Bank placeholders (Q1–Q3) and the real email layout (Q6, Q8, Q9, Q11) are still unknown: everything is built and tested against fake values. This file summarizes the parts that are easiest to get wrong; when they disagree, `SPEC.md` wins.

## Commands

Run from the repo root unless noted. Server tests need the throwaway Postgres/Redis containers.

```sh
make services                  # start test Postgres (127.0.0.1:55442) + Redis (:56389)
make lint typecheck security   # ruff + format check, mypy --strict, bandit + pip-audit
make test                      # full pytest suite
make coverage                  # tests + per-package ≥95% gate (scripts/coverage_gate.py)
make cashier-check             # cashier: eslint, tsc (app + service worker), vitest, npm audit
make e2e                       # Playwright screenshots (builds cashier, renders admin fixtures)
make sync-assets               # after editing shared/tokens.css or shared/fonts
make agent-check               # desktop agent: capability guard, rustfmt, clippy, cargo test (Rust on PATH)
cd server && uv run pytest tests/unit/test_foundation.py::test_tokens   # single test
cd server && uv run pytest -m "invariant"                                # SPEC §3 invariant tests only
cd cashier && npx vitest run src/format.test.ts                         # single cashier test file
```

- Python deps are managed with `uv` (`server/uv.lock`); never pip-install into the project. The cashier pins exact npm versions and overrides `minimatch` to a patched release.
- pytest runs with sockets disabled except localhost (pytest-socket); DKIM DNS goes through a stub in tests. Tests use `tests/fixtures/world.py` (a migrated DB, app role under RLS, per-test tenant "worlds") and `tests/fixtures/api.py` (in-process HTTP harness for both hosts).
- Every SPEC §3 invariant has tests in `server/tests/invariants/`; none are xfail any more. New features touching an invariant must extend those tests.
- Shared amount/time formatting is pinned by `shared/format_vectors.json`, used by both pytest and vitest.
- Writing `.env*` files is blocked by the user's permission settings, and a hook blocks Bash commands that mention `.env` (write such text with the Write tool). Configuration variables are documented in `docs/configuration.md`; the user creates the env file themselves and never pastes secrets or real bank emails into chat.

## Code map

- `server/src/khugjil/ingest/`: `checks.evaluate()` is the pure b–g pipeline; `pipeline.ingest()` adds check a and persistence; `smtp.py` and `api_v1/hooks.py` are the two transports.
- `server/src/khugjil/db/`: models + migration `0001` (authoritative DDL, RLS policies, SECURITY DEFINER lookups). All tenant data access goes through `tenancy.tenant_session()`; cross-tenant lookups only through `tenancy.Resolver`.
- `server/src/khugjil/routing/eligibility.py` (pure) + `delivery/service.py` (fanout/send with re-check) + `delivery/channels.py` (SSE, Web Push, Telegram).
- `server/src/khugjil/web/app.py` dispatches by Host to the cashier API (`api_v1/`) and the admin panel (`admin/`).
- `cashier/src/` is framework-free TypeScript; `dom.h()` is the only way to build elements (ESLint bans innerHTML). `CashierApp` takes a `Platform` (`platform-web.ts` for the browser, `platform-agent.ts` for the Tauri agent); `vite.agent.config.ts` builds `agent.html` + `overlay.html` into `dist-agent/`.
- `agent/src-tauri/` is the Tauri v2 agent. The webview never sees the device token: `routes.rs` allowlists API calls, `api.rs` adds the bearer token, `stream.rs` holds SSE. Capabilities are per window and guarded by `agent/scripts/check-capabilities.mjs`. Server side: bearer auth only for `kind = agent` devices, behind `KHJ_AGENT_DEVICES_ENABLED` (tests in `server/tests/invariants/test_inv09_agent_devices.py`).

## Required workflow (SPEC §12)

1. **First deliverable is `PLAN.md`, then STOP for approval.** It must contain: architecture, data model, STRIDE threat model, ASCII wireframes of the cashier view (full and compact) and the main admin pages, and open questions. Write no implementation code until the user approves the plan.
2. After approval, write the security-invariant tests **first**. Then implement Phase 1 in small commits, in this order: server core → admin panel → cashier view → Telegram.
3. **Phase 2 (Tauri desktop agent)** is planned in `PLAN-PHASE2.md` and built on the `phase2` branch. The cashier UI stays a standalone bundle that talks to the server only through the versioned JSON API plus SSE; the agent swaps only the platform adapters.
4. **Never invent `[PLACEHOLDER]` values.** These include the Khan Bank sender address, the subject pattern, the DKIM `d=` domain, the interbank-incoming format, whether port 25 is blocked, and the retention period. Ask the user.
5. Never put real account numbers, names or emails in code, tests, fixtures or commits. Use only fake data, like the sample in SPEC §4.
6. A `README.md` is required, with parts in Mongolian: the Gmail filter guide and cashier PC setup, including the Windows 11 full-screen notification limitation.

## Stack (SPEC §11)

- **Server:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, PostgreSQL, Redis + arq (background jobs), aiosmtpd (inbound SMTP on :25), pywebpush (VAPID), pydantic-settings, structlog with a redaction filter, libsodium field encryption.
- **Admin panel:** server-rendered Jinja + HTMX.
- **Cashier view:** a standalone TypeScript + Vite static bundle served by Caddy, with minimal dependencies and no UI kit. It must stay decoupled from server rendering so a Tauri shell can load it unchanged later.
- **Ops:** Docker Compose, Caddy for TLS, non-root containers with read-only filesystems, only ports 25 and 443 exposed.
- **CI gates:** `ruff`, `mypy --strict`, `bandit`, `pip-audit`, `npm audit`, `pytest`, Playwright screenshot tests. Coverage must be **≥95%** on the parser, filters, routing, consent, on-duty logic and tenant isolation.

## Architecture: the email pipeline

Data flows like this: tenant's Gmail forwarding filter → per-tenant secret inbound address (`<random 16+ chars>@in.<DOMAIN>`) → aiosmtpd. If port 25 is blocked, the fallback path is Cloudflare Email Routing → a Worker → an HMAC-signed, timestamped HTTPS POST into the **same** pipeline. The pipeline then runs: validation chain → `khan_v1` parser → routing → Notifier fan-out.

**Validation runs in this fixed order. Any failure drops the email.** The only thing logged is `tenant_id`, `reason_code` and `sha256(Message-ID)`.
1. The envelope RCPT is an active tenant inbound address.
2. The original From is on the allowlist.
3. DKIM passes for the bank's domain, OR an ARC chain from google.com verifies and records dkim=pass.
4. There are no attachments and the size is ≤256 KB.
5. The subject and body match the strict template.
6. The Кт/To account belongs to the tenant AND the Дт/From account does not (own transfers are skipped).
7. The Дт amount equals the Кт amount, in the same currency, and is > 0.

The deny-list (OTP, код, нууц үг, …) is checked on the subject and the body, **excluding** the payer-written description.

**Parser (`khan_v1`, versioned):** first normalize the HTML table to text by cell and row. Then parse anchored sections in a fixed order. Treat Latin `K` and Cyrillic `К` in "Кт" as the same. Capture the description **last**, strictly between its label and "Харилцагч танд баярлалаа", so injected label-like text can't affect other fields. Use `Decimal` for amounts and the `Asia/Ulaanbaatar` timezone. If the sender and DKIM are valid but the template doesn't match, alert the owner with no email content. Fuzz the parser with hypothesis.

**Delivery gate:** a transaction is delivered only when ALL of these hold: the account is active, the group is active, the device or member is paired or verified, the member has active versioned consent, and the member is on duty. Off-duty means nothing is sent and **nothing is queued**. The idempotency key is `HMAC(tenant_id + journal_no + system_date + to_account)`, giving exactly-once delivery per device and channel.

**Channels** sit behind a `Notifier` interface, so Viber or SMS can be added later without implementing them now:
- **Cashier PWA (primary):** SSE with a 15s heartbeat and backoff reconnect, refetching items missed during the shift. In-page toast, chime, Web Push when the page is unfocused, and title flashing. Pairing uses one-time codes that are ≥128-bit, single use and valid 10 min, and binds a device credential to an httpOnly SameSite=Strict cookie. Revoking a device closes its SSE stream. Shifts start and end with a member PIN (argon2id).
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

## UI constraints (SPEC §7–8)

- The notification text is identical on every channel and has no emoji. Amounts are formatted `1,234,567 ₮` with decimals only when non-zero, and other currencies show their ISO code.
- The visual style is calm and ledger-like and must not look AI-generated: no emoji, gradients, glassmorphism, glow or illustrations. Use 1px borders instead of shadows, a radius of 6px or less, an 8px grid, IBM Plex Sans (self-hosted), tabular numerals, and right-aligned amounts. There is one accent color, chosen per tenant: #1F5E3B or #1D3557.
- Microcopy is formal Mongolian ("Та") with no exclamation marks.
- The cashier view shows **only** what SPEC §8.3 lists: no navigation, no settings, no more than 10 items of history, and no export.
- Design tokens live in one CSS-variables file shared by the admin panel and the cashier view. `/dev/ui` is the component inventory page and is disabled in production.
