# PLAN.md: Khan Bank Income Notifier, Phase 1

> **Status:** draft for approval. No application code has been written.
> `SPEC.md` is the source of truth. Where this plan has to interpret something, it is marked **(confirm: Qn)** and listed in §6.
> Phase 2 (the Tauri desktop agent) is only designed for here, not planned in detail.

Contents: 1 Architecture · 2 Data model · 3 STRIDE threat model · 4 Wireframes · 5 Implementation order · 6 Open questions

---

## 1. Architecture

### 1.1 Overview

```text
Tenant's Gmail (forwarding filter: Khan Bank notifications only)
   |
   |  SMTP to <secret>@in.<DOMAIN>, port 25, STARTTLS required
   |  (fallback only if port 25 is unusable, Q4:
   |   Cloudflare Email Routing -> Worker -> HTTPS POST /hooks/cf, HMAC + timestamp)
   v
smtp (aiosmtpd) -- in-memory pipeline: checks a..g -> khan_v1 parser -> INSERT transaction
                                                                            |
                                                         PostgreSQL (row-level security per tenant)
                                                                            |
                         arq job fanout(txn) via Redis  <-------------------+
                                     |
worker -- eligibility -> delivery rows (unique) -> per-channel send
             |-- SSE:       Redis pub/sub -> api process holding that device's stream
             |-- Web Push:  VAPID, RFC 8291 encrypted -> browser push service -> service worker
             `-- Telegram:  Bot API (owners only)

caddy :443 --+-- app.<DOMAIN>    static cashier bundle, /api/v1, SSE, /hooks/*  -> api
             `-- admin.<DOMAIN>  admin panel (Jinja + HTMX)                     -> api
```

### 1.2 Processes and hostnames

| Process | Role | Exposed |
|---|---|---|
| `caddy` | Terminates TLS (certificates via TLS-ALPN-01, so port 80 stays closed), serves the cashier bundle, reverse proxy, security headers | 443 |
| `smtp` | aiosmtpd. Inbound only, never relays. Runs the ingest pipeline in-process. | host 25 → container 2525 (no root, no capabilities) |
| `api` | FastAPI: cashier JSON API + SSE, admin panel, Telegram webhook, Cloudflare ingest endpoint | internal |
| `worker` | arq: fan-out, sends, retries, cron jobs | none |
| `postgres`, `redis` | Data; job queue, pub/sub, rate-limit counters | internal network only |

Hostnames (confirm: Q7):

- `app.<DOMAIN>`: cashier PWA, `/api/v1`, webhooks.
- `admin.<DOMAIN>`: admin panel, plus invite and consent pages for roles that sign in.
- `in.<DOMAIN>`: A record to this host and `MX in.<DOMAIN>` → `in.<DOMAIN>`. SPF `v=spf1 -all` and DMARC `p=reject`, because this domain never sends mail.

The admin panel and the cashier view are on **separate origins**. That way the device credential and the admin session never share cookies or storage, a bug on one origin cannot read the other, and each origin gets its own strict CSP. All cookies use the `__Host-` prefix (Secure, `Path=/`, no `Domain`).

### 1.3 Ingestion pipeline

One transport-agnostic function, `ingest(envelope_rcpt, raw_bytes, transport) -> Outcome`, is called by `smtp` and by `/hooks/cf`. The raw message exists only in that call's memory. It is never written to disk, Redis, the job queue, logs or exception reports. The only thing that leaves the function is a `transaction` row containing the allowed fields.

**SMTP layer**

- STARTTLS is required before `MAIL FROM`. The certificate for `in.<DOMAIN>` is read-only from Caddy's storage and reloaded when it is renewed. An optional MTA-STS policy makes Gmail insist on valid TLS.
- Exactly one `RCPT` is allowed per session. An unknown or retired address is rejected with `550` at RCPT time (check a). The lookup uses an HMAC of the local part, so the database never needs the plaintext address for matching.
- `SIZE` is advertised as 256 KB, and `DATA` beyond that is aborted with `552`. This is a memory bound enforced while streaming. Its reason code is `TOO_LARGE`.
- Per-IP limits (connections, sessions per minute, failed RCPTs) are kept in Redis. Connection log lines contain only the IP and the SMTP reply code, never addresses or content, and they feed fail2ban (confirm: Q15).
- After `DATA`, a content-level drop still gets **`250`**. Rejecting at that point would make Gmail send bounces to the tenant and could get forwarding disabled. Only the RCPT check rejects.

**Ordered checks.** The first failure stops processing. The only record written is `ingest_event(tenant_id, reason_code, sha256(Message-ID), received_at)`.

| Step | Check | Reason codes |
|---|---|---|
| a | Envelope RCPT is an active tenant inbound address | SMTP 550. No event is written because no tenant is known. |
| — | Gmail forwarding-confirmation branch (below) | `FWD_CONFIRM_STORED` |
| b | Exactly one `From` header, and its address ∈ `BANK_SENDERS` | `HEADER_DUPLICATE`, `FROM_NOT_ALLOWED` |
| c | DKIM passes with `d` ∈ `BANK_DKIM_DOMAINS`, OR the ARC chain validates, is sealed by google.com, and records `dkim=pass header.d=<bank domain>` | `AUTH_FAIL` |
| d | Leaf MIME parts are only `text/plain` or `text/html` (confirm: Q8); raw and decoded size ≤ 256 KB; MIME depth ≤ 5 | `ATTACHMENT`, `TOO_LARGE`, `MIME_INVALID` |
| e | In this order: deny-list on the subject → subject matches `BANK_SUBJECT_RE` → body matches the `khan_v1` template → deny-list on the body minus the description | `DENYLIST`, `SUBJECT_MISMATCH`, `TEMPLATE_MISMATCH` |
| f | The Кт account is an active tenant account, and the Дт account is not a tenant account (unless the tenant enabled own transfers, Q35) | `NOT_TENANT_ACCOUNT`, `ACCOUNT_INACTIVE`, `OWN_TRANSFER` |
| g | Дт amount = Кт amount, same currency, amount > 0 | `AMOUNT_MISMATCH` |
| — | Idempotency key already exists | `DUPLICATE` |
| — | Stored | `ACCEPTED` |

**DKIM and ARC details**

- Verification uses dkimpy, with an injectable DNS function: a stub in tests and the system resolver in production.
- Signatures with an `l=` (body length) tag are rejected, because `l=` lets anyone append content.
- The signed headers (`h=`) must include `From`, `Subject` and `Date` (and `To` if Q13 is approved). A duplicate `From`, `Subject`, `Date` or `To` header gives `HEADER_DUPLICATE`, which closes the classic header-duplication bypass. Expiry (`x=`) is honoured.
- ARC uses dkimpy's `arc_verify`. The chain must validate, and only the **highest-instance** set is trusted, and only if its ARC-Seal is `d=google.com`. Its `ARC-Authentication-Results` (authserv-id `mx.google.com`) must contain `dkim=pass` with `header.d` *exactly* equal to a configured bank domain. Earlier sets are ignored.

**Deny-list.** Text is normalized with NFKC and casefold, then matched on token boundaries. So `PIN` matches, but a masked name such as `PINO***` does not. The word list from SPEC §3.3 lives in code, not configuration.

**Forwarding-confirmation branch** (between a and b). The email must be `From: forwarding-noreply@google.com`, pass DKIM with `d=google.com`, and match a strict template. Then only the confirmation code is stored, encrypted, with `expires_at = received_at + 24h`, and it is visible only to owners (confirm: Q17). No link is stored or followed. Any other email continues to check b and is dropped there.

**Template-change alert.** If checks b and c pass but e fails with `SUBJECT_MISMATCH` or `TEMPLATE_MISMATCH`, the owner gets an alert: "Имэйлийн формат өөрчлөгдсөн байж магадгүй". It carries no content and fires at most once per 24 h per tenant. An OTP email is stopped earlier by the subject deny-list, so it never triggers this alert.

### 1.4 Parser `khan_v1` and sanitizer

- **HTML to text.** A subclass of the standard library's `html.parser` emits one line per table row, with cells separated by tabs. `<script>`, `<style>`, `<img>`, all attributes and all links are ignored, so no URL is ever read. Node and line counts are hard-capped. If only a `text/plain` part exists, that part is used (Q8).
- **Normalization before matching.** NFC; remove zero-width and bidi characters; treat Latin `K` and Cyrillic `К` in the `Кт` label as the same; collapse whitespace.
- **Anchored, fixed-order sections:** header (`Огноо/Date`, `Журналын/Journal No`, `Системийн огноо/System Date`) → Дт block → Кт block → description. Each pattern is anchored per line, has bounded quantifiers and no nested repetition, so there is no ReDoS risk.
- **What is captured:** `txn_at`, `journal_no`, `system_date`, `from_account`, `from_amount`, `from_currency`, `to_account`, `to_amount`, `to_currency`, `description_raw`.
- **What is never captured.** These are matched with non-capturing patterns only: payer name, seller name, rate, amount in words, bank name, and any balance line (Q11).
- **Account numbers.** Both are wrapped in `Sensitive`, whose `repr` and `str` return `<redacted>`. The raw strings are reduced to HMACs inside check f and then dropped. The payer account is never stored.
- **Description.** It is captured **last**, strictly between `Гүйлгээний утга/Transaction description:` and `Харилцагч танд баярлалаа`, so label-like text inside it cannot move other fields.
- **Value formats.** Amounts must match `^\d{1,3}(,\d{3})*\.\d{2}$` and become `Decimal`. Currency must match `^[A-Z]{3}$`. Datetimes use `%Y.%m.%d %H:%M:%S` in `Asia/Ulaanbaatar` and are stored as `timestamptz`.
- **Account canonicalization** is the same for email text and admin input: digits only. The IBAN form is handled once Q9 is answered.
- **Versioning.** A parser registry is keyed by version. The `parser_version` that produced each transaction is stored with it.

**Description sanitizer** (applied once at ingest):

1. NFC normalize.
2. Remove Unicode categories Cc (control), Cf (format: zero-width characters, bidi embeddings, overrides and isolates, BOM, invisible operators) and Co (private use), plus noncharacters.
3. Turn the remaining whitespace into single spaces, then trim.
4. Truncate to 200 code points and drop any dangling combining marks.

The result is plain text everywhere. Nothing ever interprets it as HTML, Markdown or a URL.

### 1.5 Routing and delivery

**Eligibility** is one pure function, `eligible_targets(txn, now)`, and it is the most heavily tested code in the project.

*Device target* (web now, agent later). A transaction goes to device D, whose open shift S belongs to member M, only if ALL of these hold:

- the bank account is active;
- some group G is active, contains the account, contains D and contains M (confirm: Q21);
- D is active (paired, not revoked), and its kind is enabled (`agent` is disabled in Phase 1);
- S is open on D and was started with M's PIN, and M is active;
- M has an active consent given on the web (confirm: Q31);
- M is on duty: S is open, and if M has a duty schedule, `now` falls inside it (confirm: Q24).

*Telegram target.* An owner M with a linked chat gets the transaction only if: the account is active; some active group contains both the account and M; M is active; M has an active consent given in Telegram; and M is on duty by schedule (no schedule means always on duty, confirm: Q24).

Off duty means there is no target, so no delivery row is created and nothing is queued (SPEC §6.5).

**Flow**

1. Ingest commits the `transaction` (unique `idempotency_key`) and enqueues `fanout(txn_id)`.
2. `fanout` computes the targets and inserts `delivery(transaction_id, channel, target_id)` rows with `ON CONFLICT DO NOTHING`. This gives exactly-once delivery per device and channel even if `fanout` runs twice.
3. For each new row, `send(delivery_id)` **re-checks eligibility immediately before sending**. If consent was revoked or the shift ended in between, the row becomes `skipped`. Otherwise the row goes to that channel's Notifier.
4. Telegram and Web Push retry with backoff. Persistent failures appear on Тойм.

The **idempotency key** is `HMAC-SHA256(k_idem, lp(tenant_id) ‖ lp(journal_no) ‖ lp(system_date) ‖ lp(to_account))`, where `lp` is a length prefix. The prefix prevents two different inputs from concatenating to the same string.

**Notifier interface.** Viber and SMS can be added later without changing callers.

```python
class Notifier(Protocol):
    channel: ClassVar[Channel]          # sse | webpush | telegram
    async def send(self, target: Target, note: Notification) -> SendResult: ...
```

`format.build(txn, tenant)` builds the `Notification` once, following SPEC §7. Telegram and Web Push get it as plain text. The cashier gets structured fields plus the same strings. A shared file, `shared/format_vectors.json`, is run by both pytest and vitest, so the server and the cashier format amounts identically (`2,800 ₮`, `2,800.50 ₮`, `250 USD`).

**Channels**

- **SSE.** The worker publishes to the Redis channel `dev:{device_id}`. The api process holding that device's stream sends `event: txn` with `id: <transaction.seq>`. The client acknowledges with `POST /api/v1/transactions/{id}/ack`, which marks the delivery `acked`.
  - A heartbeat comment goes out every 15 s. On every heartbeat the server re-checks the device and shift as a backstop for revocation events.
  - At most 2 concurrent streams are allowed per device.
- **Web Push.** The payload is encrypted per RFC 8291, so push services see only metadata. A push is sent for each device delivery whose shift has a subscription.
  - The service worker shows an OS notification only when no cashier window is focused (checked with `clients.matchAll`).
  - It uses `tag = transaction id`, so duplicates collapse, and clicking the notification focuses the cashier page.
  - How each browser treats a push that shows no notification will be verified during implementation. If a browser objects, the fallback is to always show the notification with the same tag.
- **Telegram.** `sendMessage` is called with no `parse_mode`, link previews disabled, and the plain text from SPEC §7 (URL defanging: Q16).

### 1.6 Cashier frontend (`cashier/`)

- **Stack.** TypeScript + Vite with no runtime framework: a roughly 50-line DOM helper that uses only `createElement` and `textContent`.
- **XSS guard.** The CSP includes `require-trusted-types-for 'script'`, so an accidental `innerHTML` throws at runtime. ESLint also bans `innerHTML`, `outerHTML`, `insertAdjacentHTML` and `document.write`.
- **Modules**
  - `api/`: versioned client with an injectable auth transport (a cookie now, a bearer token for the Phase 2 agent).
  - `stream.ts`: EventSource, reconnect with exponential backoff and jitter, then `GET /api/v1/transactions?since=<last id>` limited to the current shift.
  - `ui/`: top bar, hero, list, verify box, toast and banners.
  - `audio.ts`: a chime synthesized with WebAudio, so no audio file is needed. The "Ээлж эхлүүлэх" click unlocks it.
  - `voice.ts`: Mongolian number words, generated by us, mapped to a sequence of clips (Q30).
  - `push.ts`, `sw.ts`: a hand-written service worker for the app-shell cache, push and notification clicks.
  - `title.ts`: title flashing until the page gets focus.
- **Behaviour** follows SPEC §6.1, §8.2 and §8.3 exactly.
  - The red connection bar appears after 5 s of disconnection.
  - A new item is highlighted for 10 s.
  - Toasts last 8 s by default (5–20 s tenant setting). Hover pauses them. At most 3 stack, and the rest collapse into "+N бусад".
  - `prefers-reduced-motion` is honoured.
- **Layouts:** full at ≥1024 px, compact at 360–480 px, and a phone layout for the PWA. Hit targets are ≥40 px.

### 1.7 Admin panel

- **Templates.** Jinja2 with autoescape on. A test greps the templates and fails on any `|safe`.
- **HTMX** is vendored and pinned, with `allowEval = false` and `includeIndicatorStyles = false`, and no `hx-on`. All JavaScript and CSS come from `/static`; there are no inline scripts or styles.
- **CSRF.** The session cookie is SameSite=Strict, and a per-session synchronizer token sits in a `<meta>` tag. HTMX sends it as a header, and forms send it as a field. The `Origin` header must also match.
- **Confirmations.** Every destructive action uses a confirm dialog that names the object.
- **TOTP step-up.** CSV export, inbound-address rotation, role changes and owner-only settings require a TOTP check within the last 5 minutes.
- **QR codes** are rendered server-side as SVG (segno) and served from a same-origin URL.
- **`/dev/ui`** (the component inventory) is mounted only when `ENV=development`. Startup refuses a production config that has dev routes enabled, and a test asserts the route returns 404 in production.

### 1.8 Identity, auth and consent

**Roles** (confirm: Q20)

| Capability | owner | admin | viewer | cashier |
|---|:-:|:-:|:-:|:-:|
| Admin panel sign-in (password + TOTP) | ✓ | ✓ | ✓ | – |
| Тойм, Гүйлгээний түүх | ✓ | ✓ | ✓ | – |
| CSV export (audit-logged) | ✓ | ✓ | – | – |
| Accounts, groups, devices, cashiers, settings | ✓ | ✓ | – | – |
| Manage owners, admins, viewers | ✓ | – | – | – |
| Имэйл холболт (address, forwarding code, rotation) | ✓ | – | – | – |
| Service terms | ✓ | – | – | – |
| Аудит лог | ✓ | ✓ | – | – |
| Telegram notifications | ✓ | – | – | – |
| Work a shift on a paired device (PIN) | ✓ | ✓ | – | ✓ |

A tenant always has at least one active owner; this is enforced in code and tested.

- **Tenant creation.** An operator CLI (`khugjil tenant create`) prints an owner invite link. There is no public sign-up (Q18).
- **Invites.** Single use, valid 72 h. The token is in the URL **fragment**, so it never reaches server logs or `Referer` headers, and is redeemed by a POST.
  - Roles that sign in set a password, enroll TOTP and receive 10 recovery codes.
  - Cashiers set a 6-digit PIN.
  - Anyone who will receive notifications reads and accepts the consent page.
- **Passwords.** argon2id (`m=64 MiB, t=3, p=1`), minimum 12 characters, checked against a bundled common-password list. There is no outbound email, so an owner resets admin, viewer and cashier credentials, and the operator CLI resets owners (Q19).
- **Admin sessions.** An opaque 256-bit token, stored as SHA-256. Idle timeout 30 min, absolute timeout 12 h. The token is rotated on sign-in and on step-up.
- **TOTP.** RFC 6238 (pyotp). The secret is encrypted at rest. A ±1 time step is accepted, and reusing the last accepted step is refused.
- **Device pairing**
  1. An admin picks a device name and groups, which creates a `pairing_code` with two forms:
     - a 256-bit token, carried in a QR code as `https://app.<DOMAIN>/pair#t=<token>`;
     - a short code `XXXX-XXXX-XXXX` (Crockford base32, 60 bits, Q25).
  2. Both forms are single use and bound to the tenant. They expire after 10 min. At most 5 wrong short-code attempts are allowed per code and per IP.
  3. Redeeming the code sets `__Host-khj_dev` (HttpOnly, Secure, SameSite=Strict) to a 256-bit device token. The token is stored hashed and rolled every 30 days.
  4. Revoking a device deletes its credentials and publishes `revoke:{device_id}`. Its stream closes immediately, or within one heartbeat at worst.
- **PIN.** 6 digits, argon2id, with trivial PINs refused. It only works on a paired device of the same tenant. After 5 failures the member is locked out for 15 min across all devices, escalating on repeat. An owner or admin can reset it.
- **Shifts.** One open shift per device, enforced by a partial unique index.
  - A shift ends on: the "Ээлж дуусгах" button, N hours of inactivity (the client reports interaction at most every 5 min), the scheduled end time, member or device revocation, or consent revocation.
  - Ending a shift deletes that member's push subscriptions on that device and tells the page to reset.
- **Consent**
  - The texts are versioned in `consent_document(kind, version, body, sha256)`.
  - Each acceptance row stores `version`, `accepted_at`, `channel` (`web` or `telegram`) and `revoked_at`.
  - *Active* means the latest non-revoked row whose version is at least the tenant's minimum.
  - Consent can be revoked from the admin panel, from a link on the consent page, or with `/stop` in Telegram. Revocation immediately ends shifts and removes push subscriptions. Consent is also checked again right before every send.
- **Tenant terms.** The owner's acceptance is stored with the version, the time, and the explicit confirmation that they may share this data with staff (SPEC §9).

### 1.9 Cryptography and keys

- **Keys.** PyNaCl (libsodium). `MASTER_KEY` is 32 bytes from the `.env` file. Subkeys come from `crypto_kdf` with fixed contexts: field encryption, account HMAC, journal HMAC, idempotency, inbound-address HMAC and Telegram-chat HMAC.
- **Field encryption** uses XChaCha20-Poly1305 with `tenant_id`, table and column as associated data. A ciphertext copied into another tenant's row therefore fails to decrypt.
- **Encrypted fields:** account numbers, inbound local parts, forwarding codes, TOTP secrets, push endpoints and Telegram chat ids. Transaction descriptions are also encrypted; this is beyond the spec and needs your approval (Q14).
- **Tenant-scoped HMACs.** Most HMACs are `HMAC(k, tenant_id ‖ value)`, so the same account number in two tenants cannot be linked. Inbound addresses and Telegram chat ids use a global key instead, because the tenant is not known yet when they are looked up.
- **Rotation.** `key_version` columns allow key rotation. Account numbers are encrypted, not only hashed, so their HMACs can be recomputed under a new key.
- **Secrets** exist only in the `.env` file, **which you create**. They are read through pydantic-settings as `SecretStr`. The repo gets a `.env.example` with variable names only, plus `khugjil keys generate`, which you run locally to print fresh values. I never need to see them.
- **Bank settings.** `BANK_SENDERS`, `BANK_DKIM_DOMAINS` and `BANK_SUBJECT_RE` have **no defaults**, and the app refuses to start without them. Development and tests use fake values (`bank.example`).

### 1.10 Logging

- structlog writes JSON through an **allowlist** processor. Only known keys pass (`event`, `tenant_id`, `reason_code`, `msgid_sha256`, request id, route, status, duration, internal ids), and everything else is dropped.
- Exceptions are logged without local variables. The pipeline raises only typed errors that carry reason codes, never message text.
- The uvicorn access log is replaced by our own, which logs the path without the query string.
- **Test:** run the full pipeline and every API route with marker strings in every sensitive field, capture logs and stderr, and assert that no marker appears.

### 1.11 Background jobs (arq, Asia/Ulaanbaatar)

| Job | When |
|---|---|
| `fanout`, `send` | On demand |
| Health check: no `ACCEPTED` email for N business hours → owner alert (Q27) | Every 10 min |
| Shift auto-end (inactivity, scheduled end, end of duty window) | Every minute |
| Device online state; delivery-failure summary for Тойм | Every minute |
| Purge: transactions and deliveries past the retention period (Q5), `ingest_event` > 30 d, expired codes, invites and sessions, forwarding codes > 24 h | Hourly |

Owner alerts appear in the admin panel and, if linked, in Telegram (Q27).

### 1.12 Phase 2 readiness (design only)

- The cashier bundle talks only to `/api/v1` (a published, versioned OpenAPI schema) plus SSE. Nothing is server-rendered, so a Tauri shell can load it unchanged.
- `device.kind` is `web` or `agent`. The auth dependency already resolves a device from either the cookie (web) or an `Authorization: Bearer` token (agent). Bearer auth is disabled by a flag in Phase 1. It is needed because a Tauri webview runs on its own origin, where SameSite=Strict cookies are not sent.
- The cashier's transport and notification layers are injectable, so the agent can swap in token auth and its own overlay window without UI changes.

### 1.13 Repository layout

```text
server/                      Python package `khugjil`
  src/khugjil/
    settings.py  logging.py  crypto.py  cli.py
    db/          models, tenant-scoped session helper (RLS), alembic/
    ingest/      smtp.py  cf_hook.py  pipeline.py  dkim_arc.py  mime.py
                 denylist.py  sanitize.py  forwarding.py  parsers/khan_v1.py
    routing/     eligibility.py  duty.py  consent.py
    delivery/    notifier.py  format.py  sse.py  webpush.py  telegram.py
    auth/        passwords.py  totp.py  sessions.py  csrf.py  pairing.py  pin.py
    api_v1/      cashier JSON API + SSE
    admin/       routes, templates/, static/
    jobs/        arq worker + cron
  tests/
    invariants/  one module per SPEC §3 item
    fixtures/    fake-only email builder, test DKIM/ARC keys, stub DNS
cashier/                     TypeScript + Vite PWA
shared/                      tokens.css, format_vectors.json, fonts/ (IBM Plex Sans woff2, Latin + Cyrillic)
deploy/                      docker-compose.yml, Caddyfile, ufw.sh, fail2ban/, backup.sh
cf-worker/                   only if Q4 requires the fallback
e2e/                         Playwright flows + screenshot tests
```

The fake email builder renders the SPEC §4 sample (and variants) to HTML, signs it with a **test** DKIM key served by the stub DNS, and can wrap it in a fake Gmail ARC set signed by a test "google.com" key. No real email is needed for any test.

### 1.14 Tooling and CI

- **Dependencies.** Python uses `uv` (lockfile with hashes) and Node uses `npm ci`.
  - Runtime: fastapi, uvicorn, sqlalchemy, alembic, psycopg, redis, arq, aiosmtpd, dkimpy, pynacl, argon2-cffi, pyotp, pywebpush, pydantic-settings, structlog, jinja2, segno, httpx.
  - Dev: pytest, pytest-asyncio, hypothesis, pytest-socket, coverage, ruff, mypy, bandit, pip-audit.
- **Test environment.** Tests run against real PostgreSQL and Redis (a compose `test` profile), because RLS cannot be tested on SQLite. The network is disabled during tests (pytest-socket), and DKIM DNS goes to the stub.
- **CI:** ruff, `mypy --strict`, bandit, pip-audit, npm audit, eslint, tsc, pytest (with hypothesis fuzzing of the parser and sanitizer), vitest, Playwright.
- **Coverage gate.** ≥95% for the parser, filters, routing, consent, on-duty and tenant-isolation modules. A small script checks this per package from `coverage.json`, because coverage.py only supports one global threshold.
- Concrete commands go into `CLAUDE.md` once the project is scaffolded.

### 1.15 Deployment

- **Containers.** Docker Compose. Every container runs as non-root with `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges` and a small `tmpfs` for `/tmp`.
- **Firewall.** ufw opens only 25 and 443; SSH is handled per Q33. fail2ban has jails for sshd, SMTP RCPT and rate-limit failures, and admin sign-in failures.
- **Caddy headers** (per origin):
  - HSTS, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and a restrictive `Permissions-Policy`. SSE is passed through without buffering.
  - Admin CSP: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`.
  - Cashier CSP: the admin CSP plus `manifest-src 'self'; worker-src 'self'; media-src 'self'; require-trusted-types-for 'script'`.
- **Backups.** A nightly `pg_dump`, encrypted with `age` to a public key whose private half stays offline. Backups are kept no longer than transactions, so purged rows age out of backups too (Q32). Keys from the `.env` file are never in the backup.

---

## 2. Data model

**Conventions**

- UUID primary keys.
- Every tenant-scoped table has `tenant_id NOT NULL`.
- Foreign keys between tenant-scoped tables are **composite** `(tenant_id, id)`, so the database itself makes cross-tenant links impossible.
- PostgreSQL RLS with `FORCE ROW LEVEL SECURITY` is on every tenant-scoped table. The policy is `tenant_id = current_setting('app.tenant_id')::uuid`, set with `SET LOCAL` in each transaction.
- The app connects as a non-owner role. Migrations run as a separate owner role.
- Lookups that must cross tenants (inbound address → tenant, Telegram chat → member, pairing token → tenant) go through narrow `SECURITY DEFINER` functions that return only ids.

Notation: `enc` = libsodium ciphertext, `hmac` = HMAC bytes.

```text
tenant                   id, name, status, accent (green|navy), include_own_transfers=false,
                         toast_seconds=8 (5..20), voice_enabled=false, voice_max_amount null,
                         show_today_totals=false, health_silence_hours, business_hours jsonb,
                         shift_idle_hours, shift_end_time null, min_consent_version, created_at
tenant_terms_acceptance  tenant_id, member_id, terms_version, staff_sharing_confirmed, accepted_at

inbound_address          id, tenant_id, local_part_hmac (globally unique), local_part_enc,
                         status (active|retired), created_at, retired_at
forwarding_confirmation  id, tenant_id, code_enc, received_at, expires_at (= +24h)
ingest_event             id, tenant_id, reason_code, msgid_sha256, received_at       -- kept 30 days

bank_account             id, tenant_id, number_enc, number_hmac, alias, status, created_at
                         UNIQUE (tenant_id, number_hmac)
recipient_group          id, tenant_id, name, status
group_account            tenant_id, group_id, account_id                             -- PK all three
member                   id, tenant_id, display_name, role (owner|admin|cashier|viewer),
                         status (invited|active|revoked), login_name null, password_hash null,
                         totp_secret_enc null, totp_last_step, pin_hash null,
                         pin_failures, pin_locked_until, created_at
group_member             tenant_id, group_id, member_id
recovery_code            tenant_id, member_id, code_hash, used_at
invite                   id, tenant_id, member_id, token_hash, expires_at (= +72h), used_at, created_by
duty_window              id, tenant_id, member_id, weekday, start_local, end_local
consent_document         kind (member|tenant_terms), version, locale, body, sha256, published_at  -- global
consent                  id, tenant_id, member_id, version, channel (web|telegram),
                         accepted_at, revoked_at, revoked_via

device                   id, tenant_id, name, kind (web|agent), status (active|revoked),
                         paired_at, last_seen_at, revoked_at, revoked_by
device_group             tenant_id, device_id, group_id
device_credential        id, tenant_id, device_id, token_hash, created_at, expires_at
pairing_code             id, tenant_id, device_name, group_ids, token_hash, short_code_hash,
                         failed_attempts, expires_at (= +10m), used_at, created_by
shift                    id, tenant_id, device_id, member_id, started_at, last_activity_at,
                         ended_at, end_reason;  UNIQUE (device_id) WHERE ended_at IS NULL
push_subscription        id, tenant_id, device_id, member_id, shift_id, endpoint_enc,
                         p256dh, auth_secret_enc, created_at
telegram_link            id, tenant_id, member_id, chat_hmac (globally unique), chat_enc,
                         status, linked_at
telegram_link_code       id, tenant_id, member_id, code_hash, expires_at, used_at

transaction              id, seq (bigint identity, used as the SSE event id), tenant_id,
                         bank_account_id null, to_account_hmac, journal_hmac,
                         idempotency_key UNIQUE, amount numeric(18,2), currency char(3),
                         txn_at timestamptz, description_enc, parser_version, received_at
delivery                 id, tenant_id, transaction_id, channel (sse|webpush|telegram), target_id,
                         status (pending|sent|acked|skipped|failed), attempts, last_error_code,
                         created_at, sent_at, acked_at;  UNIQUE (transaction_id, channel, target_id)

alert                    id, tenant_id, kind, opened_at, notified_at, resolved_at
session                  id, tenant_id, member_id, token_hash, created_at, last_seen_at,
                         expires_at, stepup_at, ip_prefix
audit_log                id, tenant_id, actor_kind (member|operator|system), actor_id, action,
                         object_type, object_id, object_label, at, ip_prefix, meta jsonb
                         -- app role has INSERT + SELECT only (append-only)
```

**Never stored anywhere:** the raw email, balance, payer name, payer account, seller name, amount in words, exchange rate and System Date. System Date is used only inside the idempotency HMAC.
`transaction` holds exactly what SPEC §3.4 allows, plus internal metadata: `seq`, `bank_account_id` (derived from the to-account HMAC), `parser_version` and `received_at`.

---

## 3. STRIDE threat model

### 3.1 Assets and trust boundaries

**Assets**, most important first:

1. **Integrity of "Орлого" notifications.** A fake one makes a cashier hand over goods, which is a direct financial loss.
2. Transaction data (amount, description, time).
3. Bank account numbers.
4. Content of tenant bank emails, including payer data that must never be kept.
5. Inbound addresses.
6. Credentials: passwords, TOTP secrets, device tokens, PINs, pairing codes and Telegram link codes.
7. Keys in the `.env` file.
8. Consent and audit records.
9. Availability of notifications.

**Trust boundaries**

| ID | From → To |
|---|---|
| TB1 | Internet → `smtp`: anyone can connect to port 25 |
| TB2 | Cloudflare Worker → `/hooks/cf` (fallback only) |
| TB3 | Cashier browser or PWA → `app.<DOMAIN>` |
| TB4 | Admin browser → `admin.<DOMAIN>` |
| TB5 | Telegram → `/hooks/telegram` |
| TB6 | Server → push services, Telegram API (outbound) |
| TB7 | App → PostgreSQL and Redis |
| TB8 | Operator → host (SSH, backups, `.env`) |

### 3.2 Threats and mitigations

**S: Spoofing**

| ID | Threat | Mitigation | Verified by |
|---|---|---|---|
| S1 | Forged bank email (spoofed `From`) to create a fake "Орлого" | From allowlist + DKIM/ARC (check c); strict template; no `l=`; duplicate headers rejected | Invariant tests 2b, 2c; DKIM fixtures signed with an untrusted key |
| S2 | Forged ARC headers claiming google.com | Cryptographic ARC verification; only the highest-instance set sealed by google.com is trusted; exact `header.d` match | Fixtures: wrong sealer, broken chain, AAR for another domain, lower-instance spoofed AAR |
| S3 | A genuine bank email forwarded or replayed by a third party to a leaked inbound address (for example a payer's own debit notice, or an old email) | 120-bit secret address with rotation; check f (Кт must be the tenant's account); idempotency; optional freshness window (Q12) and signed-`To` check (Q13) | Duplicate test; replay tests once Q12/Q13 are decided |
| S4 | Inbound address guessed | 24 lowercase base32 chars (120 bits); RCPT rate limits; fail2ban | Entropy unit test; rate-limit test |
| S5 | Forged Cloudflare ingest POST | HMAC-SHA256 over timestamp and body hash; ±300 s window; nonce replay cache in Redis; DKIM is still verified on the raw MIME | Signature, expiry and replay tests |
| S6 | Forged Telegram webhook | Constant-time `secret_token` header check plus a secret path; updates from unknown chats are ignored | Webhook auth tests |
| S7 | Stolen or guessed pairing code | Single use, 10 min, tenant-bound; 256-bit QR token carried in the URL fragment; short code limited to 5 attempts; new devices visible and revocable | Pairing tests (expiry, reuse, brute force, other tenant) |
| S8 | Admin account takeover | argon2id; mandatory TOTP; rate limits; session timeouts; step-up for sensitive actions; audit log | Auth tests |
| S9 | Someone else starting a shift on a shared PC | PIN works only on a paired device; argon2id; per-member lockout | PIN lockout tests |
| S10 | Telegram account linked by the wrong person | One-time code that expires and belongs to one member, then an explicit consent button | Link-code tests |

**T: Tampering**

| ID | Threat | Mitigation | Verified by |
|---|---|---|---|
| T1 | Label-like text in the description shifts amount or account parsing | Description captured last, between fixed anchors; anchored per-line patterns | Parser tests + hypothesis fuzzing |
| T2 | HTML or script injection through the description | Plain text everywhere; Jinja autoescape with `|safe` banned; cashier uses `textContent` only; Trusted Types; strict CSP | Jinja render test, vitest DOM test, Playwright with a `<script>` description |
| T3 | Bidi, zero-width or homoglyph tricks in the description | Cc/Cf/Co stripped; NFC; 200-character cap | Sanitizer tests (RLO, ZWJ, BOM, isolates) |
| T4 | A description that imitates an amount (for example "1,000,000 ₮ төлсөн") | Amount is visually dominant and the only thing in the amount column; description is labelled and muted | Visual review; Playwright screenshot |
| T5 | CSV formula injection | Cells starting with `= + - @ \t \r` are prefixed with `'` | CSV export test |
| T6 | Cross-tenant writes | RLS + composite foreign keys + tenant-scoped session helper | Isolation test suite |
| T7 | CSRF on admin or cashier endpoints | SameSite=Strict + synchronizer token + Origin check; the JSON API requires `application/json` | CSRF tests per route |
| T8 | Supply-chain tampering | Locked hashes; pip-audit and npm audit; minimal dependencies; no CDN | CI |

**R: Repudiation**

| ID | Threat | Mitigation |
|---|---|---|
| R1 | An admin denies revoking a device, exporting CSV or changing roles | Append-only `audit_log` (the database role cannot UPDATE or DELETE it) naming the object and actor |
| R2 | A member disputes consent | Versioned consent rows (`version`, `sha256` of the text, `accepted_at`, `channel`, `revoked_at`) |
| R3 | "The bank emailed but we were never notified" | `ingest_event` gives the reason code and Message-ID hash for every email, which can be matched without storing content |

**I: Information disclosure**

| ID | Threat | Mitigation | Verified by |
|---|---|---|---|
| I1 | Raw email leaks through logs, queues, temp files or crash reports | In-memory pipeline; only ids go into the queue; allowlist logging; no locals in tracebacks; no spooling | Marker test across logs, DB, Redis and the temp directory |
| I2 | Balance, payer name or payer account leaks | Never captured by the parser; payer account becomes an HMAC and is discarded | Marker test across DB dump, API responses, notifications and logs |
| I3 | One tenant reads another tenant's data | RLS, composite foreign keys, tenant-bound tokens and codes | Route enumeration: every endpoint with tenant B's ids returns 404 |
| I4 | Inbound address leaks | Owner-only page; rotation; health check and `ingest_event` reveal unexpected traffic | — |
| I5 | Shoulder surfing on a shared PC | Last 10 only; no history or export in the cashier view; voice threshold; shift end clears the UI and push | Playwright |
| I6 | Notification content on lock screens or in Telegram (not end-to-end encrypted) | Disclosed on the consent page; Telegram is owners only; Web Push payload is encrypted in transit | Consent text review |
| I7 | Database or backup theft | Encrypted fields with keys outside the database; age-encrypted backups; backup retention ≤ transaction retention | Restore drill |
| I8 | Secrets in the repo or in chat | Only the `.env` file, which you create; `.env.example` has names only; never requested in chat | Secret-scan step in CI |
| I9 | Tokens leak through URLs or Referer | Pairing and invite tokens in URL fragments; `Referrer-Policy: no-referrer`; access log without query strings | Log test |
| I10 | Cloudflare fallback exposes raw email to a third party | Used only if Q4 requires it; the Worker never logs or stores; documented to the owner | — |

**D: Denial of service**

| ID | Threat | Mitigation |
|---|---|---|
| D1 | SMTP flood | Per-IP connection and session limits; RCPT rejection; size cap; timeouts; fail2ban |
| D2 | Parser ReDoS or HTML bombs | 256 KB raw and decoded caps; capped MIME depth and node count; linear, anchored patterns; hypothesis fuzzing with a time budget |
| D3 | SSE connection exhaustion | Authenticated before the stream opens; at most 2 streams per device; Caddy limits |
| D4 | Attacker locks out admins | Progressive delays per account and IP rather than hard lockout for passwords; PIN lockout needs a paired device anyway |
| D5 | Forwarding silently breaks (filter deleted, Gmail pauses forwarding, address rotated) | Health-check alert after N business hours with no accepted email; "Тест илгээх" self-check |
| D6 | Telegram or push-service outage | Retries with backoff; SSE path is independent; failures shown on Тойм |
| D7 | Inbound port 25 blocked by the host | Cloudflare fallback feeding the same pipeline (Q4) |
| D8 | Disk filled by logs or the database | Log rotation; retention purge; disk alert in the health job |

**E: Elevation of privilege**

| ID | Threat | Mitigation | Verified by |
|---|---|---|---|
| E1 | Cashier or viewer reaches admin functions | Per-route role dependencies; separate origins; device credentials are not admin sessions | Role matrix test over every route |
| E2 | Admin promotes themselves or removes the last owner | Only owners manage owner, admin and viewer roles; "at least one owner" check | Tests |
| E3 | Device token used against admin routes or the reverse | Different cookie names, origins and auth dependencies | Tests |
| E4 | Container escape or host compromise | Non-root, read-only filesystem, no capabilities, `no-new-privileges`, internal network for the database and Redis | Compose lint in CI |
| E5 | SQL injection | SQLAlchemy bound parameters only; bandit | CI |
| E6 | `/dev/ui` or debug routes in production | Not mounted unless `ENV=development`; startup guard | Test |
| E7 | HTMX eval paths | `allowEval=false`, no `hx-on`, CSP without `unsafe-eval` | Template grep test |

**Residual risks** that remain by design and go into the README:

- An attacker in control of the tenant's Gmail can stop forwarding, which the health check detects, but cannot forge bank DKIM.
- A malicious owner or admin can see their own tenant's data.
- Windows 11 hides OS notification banners while another app is full-screen. The in-page view and sound still work, and Phase 2 removes this limitation.
- A customer showing a fake "payment screenshot" is outside the system's control. The verify box exists to check those claims.

### 3.3 SPEC §3 invariants → automated tests (written first)

| # | Invariant | Test (in `server/tests/invariants/`) |
|---|---|---|
| 1 | No mailbox access | The locked dependency list contains no IMAP, POP, Gmail API, Microsoft Graph or mail-OAuth package; the source tree does not import `imaplib` or `poplib`; after importing the app, neither module is in `sys.modules` |
| 2 | Checks a–g in order | One test per check. Emails that fail several checks report the earliest reason code. `ingest_event` contains only the three allowed fields. |
| 3 | Deny-list | An OTP subject is dropped; `код` in the template part of the body is dropped; `код` inside the description is accepted |
| 4 | Never extract balance, payer name or payer account | Marker values in those fields never appear in the DB, API, SSE, push payloads, Telegram text or logs |
| 5 | No raw email stored or logged | Pipeline run with a body marker: absent from logs, DB, Redis keys and the temp directory |
| 6 | Description is untrusted | Sanitizer cases (control, zero-width, bidi, 10 KB description); escaping in Jinja, the DOM and Telegram; Playwright run with a `<script>` description |
| 7 | No LLM, no link following | No LLM SDK in the lockfile; the pipeline runs with sockets disabled except stubbed DNS; HTML with links and images makes no fetches |
| 8 | Idempotency, exactly once | The same email twice gives one transaction; concurrent `fanout` gives one delivery per (channel, target) |
| 9 | Tenant isolation | Two-tenant fixture; every route probed with the other tenant's ids; a meta-test checks that every table with `tenant_id` has an RLS policy; composite foreign keys reject cross-tenant links |
| 10 | Consent | No consent gives no delivery; revoking between `fanout` and `send` gives `skipped`; revocation closes the stream and deletes push subscriptions |

---

## 4. Wireframes

Notation: `[ ... ]` is a button or input. `###` is the full-width red connection bar. `~~~` is the "sound locked" bar. `|#` is the toast's 4 px accent bar. Body text is 14 px minimum. Amounts use tabular numerals and are right-aligned in tables.

### 4.1 Cashier view, full layout (≥1024 px), tenant with 2 accounts

```text
+--------------------------------------------------------------------------------------------+
| Төв дэлгүүр · Салбар-2                  Холбогдсон       Б. Сараа   14:32   [Ээлж дуусгах] |
+----------------------------------------------------------+---------------------------------+
|                                                          |                                 |
| Сүүлийн орлого                                           | Дүнгээр шалгах                  |
|                                                          | [ 2800                        ] |
| 2,800 ₮                                                  |                                 |
| Худалдан авалт                                           | Орсон байна — 08:37,            |
| 08:37 · 2 минутын өмнө · Төв дэлгүүр                     | Худалдан авалт                  |
|                                                          |                                 |
+----------------------------------------------------------+---------------------------------+
| Цаг      |              Дүн | Утга                                       | Данс            |
| ---------+------------------+--------------------------------------------+---------------- |
| 08:37    |          2,800 ₮ | Худалдан авалт                             | Төв дэлгүүр     |
| 08:21    |         15,000 ₮ | Захиалга 4411                              | Салбар-2        |
| 08:05    |     120,500.50 ₮ | Түрээсийн төлбөр, 10-р сар                 | Төв дэлгүүр     |
| 07:58    |          3,000 ₮ | Б. Батаагийн хүүхдийн цэцэрлэгийн төлбөр,… | Салбар-2        |
| 07:41    |          250 USD | Invoice 0091                               | Төв дэлгүүр     |
| …        |                  | (сүүлийн 10 гүйлгээ хүртэл)                |                 |
+--------------------------------------------------------------------------------------------+
| Өнөөдөр: 14 гүйлгээ · 312,500 ₮                                                            |
+--------------------------------------------------------------------------------------------+
```

- **Hero:** "Сүүлийн орлого" at 13 px, muted; the amount at 48 px semibold; the description and time below; the account alias is shown only when the tenant has more than one account. The hero has an accent-tinted background for 10 s after a new item arrives.
- **List:** the last 10 transactions of the current shift (Q23). Descriptions are truncated with "…" and the full text is in a tooltip. The `Данс` column appears only when the tenant has more than one account.
- **Verify box:** when nothing matches, it shows "Сүүлийн 15 минутад ийм дүнтэй орлого ороогүй байна".
- **Footer:** today's count and total, shown only if the tenant enables it (default off).
- **Not present:** navigation, settings, export and older history.

### 4.2 Cashier view, disconnected, sound still locked

```text
+--------------------------------------------------------------------------------------------+
| Төв дэлгүүр · Салбар-2                                   Б. Сараа   14:32   [Ээлж дуусгах] |
+--------------------------------------------------------------------------------------------+
| ############### Холболт тасарсан — орлогын мэдэгдэл ирэхгүй байж болзошгүй ############### |
| ~~~~~~~~~~~~~~~~~~~~~~~~~~~ Дуу идэвхгүй — дарж идэвхжүүлнэ үү ~~~~~~~~~~~~~~~~~~~~~~~~~~~ |
+--------------------------------------------------------------------------------------------+
| (хуудасны бусад хэсэг өөрчлөгдөхгүй)                                                       |
+--------------------------------------------------------------------------------------------+
```

### 4.3 Cashier view, compact layout (360–480 px, docked beside the POS), 1 account

```text
+------------------------------------------+
| Төв дэлгүүр                   Холбогдсон |
| Б. Сараа · 14:32          [Ээлж дуусгах] |
+------------------------------------------+
| Дүнгээр шалгах                           |
| [ 2800                                 ] |
| Орсон байна — 08:37, Худалдан авалт      |
+------------------------------------------+
| Сүүлийн орлого                           |
| 2,800 ₮                                  |
| Худалдан авалт                           |
| 08:37 · 2 минутын өмнө                   |
+------------------------------------------+
| Цаг   |          Дүн | Утга              |
| ------+--------------+------------------ |
| 08:37 |      2,800 ₮ | Худалдан авалт    |
| 08:21 |     15,000 ₮ | Захиалга 4411     |
| 08:05 | 120,500.50 ₮ | Түрээсийн төлбөр… |
| 07:58 |      3,000 ₮ | Худалдан авалт    |
| …     |              |                   |
+------------------------------------------+
```

The phone (PWA) layout follows the same order with larger spacing. The verify box stays at the top.

### 4.4 Device paired, no shift running

```text
+--------------------------------------------------------------+
| Төв дэлгүүр · Салбар-2                            Холбогдсон |
+--------------------------------------------------------------+
|                                                              |
| Ээлж эхлүүлэх                                                |
|                                                              |
| Хэрэглэгч   [ Б. Сараа                       v ]             |
| ПИН код     [ ••••••                           ]             |
|                                                              |
|             [ Ээлж эхлүүлэх ]                                |
|                                                              |
| Ээлж эхлүүлснээр дуут дохио болон мэдэгдэл идэвхжинэ.        |
| Ээлж эхлээгүй үед энэ төхөөрөмжид мэдэгдэл ирэхгүй.          |
|                                                              |
+--------------------------------------------------------------+
```

The "Ээлж эхлүүлэх" click is the user gesture that unlocks audio and asks for notification permission. Only members who share an active group with this device appear in the list.

### 4.5 Toast (360×112 px, bottom-right, 16 px margin)

```text
                           +------------+
                           | +2 бусад   |
+---------------------------------------+
|# Орлого                               |
|# 2,800 ₮                              |
|# Худалдан авалт                       |
|# Төв дэлгүүр                    08:37 |
|# ======================               |
+---------------------------------------+
```

The bottom line is the remaining-time bar. Hover pauses it. The entry animation is a 150 ms slide and fade, with no bounce.

### 4.6 Admin: Тойм

```text
+--------------------+----------------------------------------------------------------------------+
| Жишээ ХХК          | Тойм                                                                       |
|                    |                                                                            |
| > Тойм             | +----------------------+ +----------------------+ +----------------------+ |
|   Дансууд          | | Банкны имэйл         | | Онлайн төхөөрөмж     | | Амжилтгүй илгээлт    | |
|   Бүлгүүд          | | Сүүлд ирсэн 08:37    | | 3 / 4                | | 0                    | |
|   Хэрэглэгчид      | | Хэвийн               | | Касс-2 офлайн        | | Сүүлийн 24 цаг       | |
|   Төхөөрөмжүүд     | +----------------------+ +----------------------+ +----------------------+ |
|   Имэйл холболт    |                                                                            |
|   Гүйлгээний түүх  | Өнөөдөр                                                                    |
|   Тохиргоо         | 42 гүйлгээ                   1,284,500 ₮                                   |
|   Аудит лог        |                                                                            |
|                    | Анхаарах асуудал                                                           |
| Д. Болд            | Асуудал                                                  |                 |
| Эзэмшигч           | ---------------------------------------------------------+---------------- |
| [Гарах]            | Касс-2 төхөөрөмж 2 цаг 10 минут холбогдоогүй байна.      |    [Юу хийх вэ] |
|                    | Г. Туяа зөвшөөрөл өгөөгүй тул мэдэгдэл авахгүй.          |    [Юу хийх вэ] |
+--------------------+----------------------------------------------------------------------------+
```

### 4.7 Admin: Имэйл холболт (owner only)

```text
+--------------------+----------------------------------------------------------------------------+
| Жишээ ХХК          | Имэйл холболт                                                              |
|                    |                                                                            |
|   Тойм             | Хүлээн авах хаяг                                                           |
|   Дансууд          | [ k7q2m4x9d3ta6vwe5hb2nrfy@in.<DOMAIN> ]  [Хуулах]  [Шинэ хаяг]            |
|   Бүлгүүд          | Төлөв: Хэвийн — сүүлийн банкны имэйл 2026.10.15 08:37                      |
|   Хэрэглэгчид      |                                                                            |
|   Төхөөрөмжүүд     | Gmail тохируулах                                                           |
| > Имэйл холболт    | [x] 1. Дамжуулах хаяг нэмэх                                        [зураг] |
|   Гүйлгээний түүх  | [ ] 2. Баталгаажуулах кодыг Gmail-д оруулна уу                             |
|   Тохиргоо         |        Код: 123 456 789   (2026.10.16 08:40 хүртэл хүчинтэй)               |
|   Аудит лог        | [ ] 3. Шүүлтүүр үүсгэх: From = <банкны хаяг>                       [зураг] |
|                    | [ ] 4. Шалгах                                                [Тест илгээх] |
| Д. Болд            |                                                                            |
| Эзэмшигч           | Сүүлд ирсэн имэйлүүд (агуулга хадгалагддаггүй)                             |
| [Гарах]            | Цаг              | Үр дүн                                                  |
|                    | -----------------+-------------------------------------------------------- |
|                    | 2026.10.15 08:37 | Хүлээн авсан                                            |
|                    | 2026.10.15 08:12 | Татгалзсан — DKIM баталгаажаагүй                        |
|                    | 2026.10.14 21:03 | Алгассан — давхардсан гүйлгээ                           |
|                    | 2026.10.14 19:40 | Алгассан — өөрийн данс хоорондын шилжүүлэг              |
+--------------------+----------------------------------------------------------------------------+
```

- **Шинэ хаяг** (rotate) asks for confirmation and TOTP step-up.
- **Тест илгээх** sends a clearly labelled test notification ("Туршилт") to this tenant's on-duty devices and Telegram without creating a transaction (Q36). The table below it shows only the time and reason code of each email that arrived, never its content.

### 4.8 Admin: Хэрэглэгчид, with a destructive-action confirm

```text
+--------------------+----------------------------------------------------------------------------+
| Жишээ ХХК          | Хэрэглэгчид                                                         [Урих] |
|                    |                                                                            |
|   Тойм             | Нэр       | Үүрэг     | Бүлэг       | Зөвшөөрөл     | Хуваарь      |       |
|   Дансууд          | ----------+-----------+-------------+---------------+--------------+------ |
|   Бүлгүүд          | Д. Болд   | Эзэмшигч  | Бүгд        | Идэвхтэй v2   | Да–Ня 09–21  | [...] |
| > Хэрэглэгчид      | Б. Сараа  | Кассчин   | Төв дэлгүүр | Идэвхтэй v2   | —            | [...] |
|   Төхөөрөмжүүд     | Г. Туяа   | Кассчин   | Салбар-2    | Хүлээгдэж буй | —            | [...] |
|   Имэйл холболт    | Н. Оюун   | Үзэгч     | —           | —             | —            | [...] |
|   Гүйлгээний түүх  |                                                                            |
|   Тохиргоо         | [...] цэс: ПИН шинэчлэх · Хуваарь · Telegram холбох · Эрх цуцлах           |
|   Аудит лог        |                                                                            |
|                    | +----------------------------------------------------+                     |
| Д. Болд            | | Г. Туяагийн эрхийг цуцлах уу?                      |                     |
| Эзэмшигч           | |                                                    |                     |
| [Гарах]            | | Идэвхтэй ээлж нь шууд дуусч, энэ хэрэглэгчид       |                     |
|                    | | мэдэгдэл цаашид ирэхгүй.                           |                     |
|                    | |                                                    |                     |
|                    | |                              [Болих]  [Эрх цуцлах] |                     |
|                    | +----------------------------------------------------+                     |
+--------------------+----------------------------------------------------------------------------+
```

### 4.9 Admin: Төхөөрөмжүүд, with the pairing dialog open

```text
+--------------------+----------------------------------------------------------------------------+
| Жишээ ХХК          | Төхөөрөмжүүд                                            [Төхөөрөмж холбох] |
|                    |                                                                            |
|   Тойм             | Нэр       | Төрөл | Бүлэг       | Сүүлд  | Төлөв               |           |
|   Дансууд          | ----------+-------+-------------+--------+---------------------+---------- |
|   Бүлгүүд          | Касс-1    | Веб   | Төв дэлгүүр | 14:31  | Онлайн · Б. Сараа   |  [Цуцлах] |
|   Хэрэглэгчид      | Касс-2    | Веб   | Салбар-2    | 12:05  | Офлайн              |  [Цуцлах] |
| > Төхөөрөмжүүд     | Утас-Болд | Веб   | Бүгд        | 14:30  | Онлайн · ээлжгүй    |  [Цуцлах] |
|   Имэйл холболт    |                                                                            |
|   Гүйлгээний түүх  | +------------------------------------------------------+                   |
|   Тохиргоо         | | Төхөөрөмж холбох                                     |                   |
|   Аудит лог        | |                                                      |                   |
|                    | | +--------+   Нэр: Касс-3     Бүлэг: Салбар-2         |                   |
| Д. Болд            | | |        |   Код: K7QM-3XRP-9DTA                     |                   |
| Эзэмшигч           | | |   QR   |   09:58 минутын дотор, нэг удаа           |                   |
| [Гарах]            | | |        |   ашиглах боломжтой.                      |                   |
|                    | | +--------+                                           |                   |
|                    | |                                                      |                   |
|                    | | Кассын компьютер дээр app.<DOMAIN>/pair хаягийг      |                   |
|                    | | нээж кодыг оруулна уу. Утсаар QR кодыг уншуулна уу.  |                   |
|                    | |                                                      |                   |
|                    | |                                               [Хаах] |                   |
|                    | +------------------------------------------------------+                   |
+--------------------+----------------------------------------------------------------------------+
```

### 4.10 Admin: Гүйлгээний түүх

```text
+--------------------+----------------------------------------------------------------------------+
| Жишээ ХХК          | Гүйлгээний түүх                                                [CSV татах] |
|                    |                                                                            |
|   Тойм             | Огноо [2026.10.01] – [2026.10.15]    Данс [Бүгд        v]                  |
|   Дансууд          | Дүн   [          ] – [          ]    [Шүүх]                                |
|   Бүлгүүд          |                                                                            |
|   Хэрэглэгчид      | Огноо, цаг       | Данс               |            Дүн | Утга              |
|   Төхөөрөмжүүд     | -----------------+--------------------+----------------+------------------ |
|   Имэйл холболт    | 2026.10.15 08:37 | Төв дэлгүүр ••2222 |        2,800 ₮ | Худалдан авалт    |
| > Гүйлгээний түүх  | 2026.10.15 08:21 | Салбар-2 ••3333    |       15,000 ₮ | Захиалга 4411     |
|   Тохиргоо         | 2026.10.15 08:05 | Төв дэлгүүр ••2222 |   120,500.50 ₮ | Түрээсийн төлбөр… |
|   Аудит лог        | 2026.10.15 07:41 | Төв дэлгүүр ••2222 |        250 USD | Invoice 0091      |
|                    |                                                                            |
| Д. Болд            | ‹ Өмнөх   1  2  3  …  26   Дараах ›                     Нийт 1,284 гүйлгээ |
| Эзэмшигч           |                                                                            |
| [Гарах]            |                                                                            |
+--------------------+----------------------------------------------------------------------------+
```

CSV export is shown only to owners and admins, requires TOTP step-up, is audit-logged, and is protected against formula injection.

---

## 5. Implementation order (after approval)

1. **Scaffold.** `git init` (Q34), uv and npm projects, compose dev and test profiles, settings, logging, crypto, CI pipeline, `.env.example`.
2. **Security invariant tests first** (§3.3). Each is marked `xfail(strict=True)` until its feature lands, so CI always shows which invariants are not enforced yet, and the marker can never be silently forgotten.
3. **Server core.** Models, migrations and RLS → ingest (smtp, checks, `khan_v1` on fake fixtures) → eligibility and routing → delivery core and SSE → jobs.
4. Admin panel.
5. Cashier view: PWA, Web Push, audio, voice.
6. Telegram.
7. README (DNS, deploy, plus the Gmail filter guide and cashier PC setup in Mongolian, including the Windows 11 full-screen limitation); Playwright screenshot tests; deploy hardening (ufw, fail2ban, backups).

All of this is done in small commits. Q1–Q3 only block **going live**, not coding, because everything is built and tested against fake values first. Phase 2 does not start without your explicit approval.

---

## 6. Open questions

**How to answer without sharing anything sensitive.** None of these needs a real email, password or token. For Q1–Q3, open a bank email in Gmail → ⋮ → "Show original". The summary at the top shows the From address and "DKIM: 'PASS' with domain …", so you can read the values off it. For structure questions (Q8–Q11), a description in your own words with fake values is enough.

**A. Spec placeholders (needed before going live, not before coding)**

- **Q1.** Khan Bank sender address or addresses for the From allowlist.
- **Q2.** The DKIM signing domain (`d=`). Does the bank sign its emails at all? If it does not, check c can never pass and we need to discuss.
- **Q3.** The exact subject line of an incoming-transfer email. Mask any digits or names in it.
- **Q4.** Port 25: which VPS provider will this run on, and does it allow **inbound** TCP 25? If not, is the domain on Cloudflare DNS, so the Email Routing fallback can be used?
- **Q5.** Transaction retention (for example 90 days). Are these also OK: `ingest_event` 30 days, audit log 1 year?
- **Q6.** Transfers from other banks: does an incoming transfer from another bank use a different layout? Which lines or labels differ? Describe with fake values.
- **Q7.** The domain name, and is it OK to use the `app.`, `admin.` and `in.` subdomains?

**B. Email structure (describe it, don't paste it)**

- **Q8.** Is the email HTML only, plain text only, or both? Does it contain images or other MIME parts, such as a logo? The plan allows only text parts. If there is an inline logo, should it be tolerated (never read), or should such emails be dropped?
- **Q9.** How do account numbers appear: 10 plain digits, IBAN (`MN…`), or masked?
- **Q10.** Does the payer receive a similar email for the same transfer? If so, is its subject different? This matters for S3.
- **Q11.** Does the email contain a balance line (Үлдэгдэл) or other lines that are not in the SPEC §4 sample? Should the UI show `Огноо/Date` or `Системийн огноо/System Date`? The plan shows `Огноо/Date` and uses System Date only in the idempotency key.

**C. Hardening beyond the spec (all stricter; approve or reject each)**

- **Q12.** Freshness: drop an email if its transaction time is more than X hours (for example 24) before it arrives. This blocks replay of old genuine emails.
- **Q13.** Require the bank-signed `To:` header to equal the tenant's registered Gmail address, which the owner enters once.
- **Q14.** Encrypt transaction descriptions at rest. This is planned as the default.
- **Q15.** Keep SMTP connection log lines (IP and reply code only, for 7 days) for fail2ban. They hold no email content, but please confirm, since the spec restricts drop logging.
- **Q16.** Telegram turns URLs into links even in plain text. Should URL-like text in descriptions be defanged for Telegram only (for example `example[.]com`)?
- **Q17.** Store only the Gmail forwarding confirmation code, or also the Gmail address that requested forwarding? The plan stores only the code.

**D. Interpretations to confirm (answer "D: defaults OK" if all are fine)**

- **Q18.** Tenants are created by the operator through a CLI. There is no public sign-up in Phase 1.
- **Q19.** There is no outbound email. Owners reset passwords for admins and viewers; the operator CLI resets owners. TOTP has recovery codes.
- **Q20.** The role matrix in §1.8: viewers sign in read-only and also need TOTP; admins cannot manage owners or admins; only owners see Имэйл холболт.
- **Q21.** A device gets a transaction only if the shift member, the device and the account share an active group.
- **Q22.** Every shift on a device requires a PIN. "Optional PIN" means members who never work shifts don't need one.
- **Q23.** The cashier list shows the last 10 transactions of the current shift, so it is empty when a shift starts. The verify box searches the last 15 minutes, even if that reaches back before the shift started.
- **Q24.** Duty. For devices, an open shift means on duty; if the member has a schedule, it also applies, and the shift auto-ends when the schedule ends. For Telegram, delivery follows the schedule, and no schedule means always on duty.
- **Q25.** The typed pairing code is 12 characters (60 bits): 10-minute expiry, single use, at most 5 attempts, next to the 256-bit QR token. Is that acceptable, or must the typed code itself be ≥128 bits (26 characters)?
- **Q26.** The Telegram bot works in private chats only and leaves every group or channel immediately.
- **Q27.** Owner alerts (forwarding silent, template changed) go to the admin panel and Telegram, with no email channel. Defaults: N = 4 business hours, with business hours Monday–Sunday 09:00–21:00.
- **Q28.** Rotating the inbound address retires the old one immediately. Gmail forwarding must then be verified again.
- **Q29.** "Masked" in Гүйлгээний түүх means the account is shown as its alias plus the last 4 digits (`••2222`). The description is shown in full.
- **Q30.** Who records the voice clips? The plan assumes a native speaker reads a list of about 40 words that I supply. Voice is used only for whole-tögrög MNT amounts; above the tenant threshold, only the chime plays.
- **Q31.** Consent is per channel. Telegram delivery needs consent given in Telegram; device delivery needs consent given on the web consent page.
- **Q32.** Where should encrypted backups go: the same host's disk, another server, or a free object store?
- **Q33.** SSH: the spec exposes only 25 and 443. Should SSH be limited to your IP, put behind a VPN such as WireGuard, or used only through the provider's console?
- **Q34.** Is it OK to `git init` when implementation starts? Which remote (GitHub?) and CI runner (GitHub Actions?) should be used?
- **Q35.** Own transfers are skipped by default, and a tenant setting can include them.
- **Q36.** "Тест илгээх" sends a labelled test notification through the real delivery path, and the email status list shows whether forwarded emails reach the server. Is that what you intended?
