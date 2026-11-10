# SPEC: Khan Bank Income Notifier (multi-tenant, security-first)

## 0. Phasing

- **Phase 1 (build now):** server + admin web panel + cashier web view (PWA)
  + optional Telegram.
- **Phase 2 (later, only after I approve):** a small Windows desktop agent
  (Tauri) that reuses the cashier web UI and adds an always-on-top overlay.
  In Phase 1, do NOT build the agent, but design the API and UI so it can be
  added without rework (see §6.4).

## 1. Goal

Tenants forward ONLY Khan Bank transfer-notification emails to this service.
It keeps ONLY incoming (credit) transactions to the tenant's registered
accounts and notifies, in real time, the consented, on-duty recipients linked
to that account. In Phase 1 the main channel is a cashier web page open in a
browser on the shop PC (or on a phone as a PWA). Admins manage everything in
a web admin panel. Everything must be free to run, apart from our own server.

## 2. Domain model

- Tenant → many BankAccounts (number stored encrypted + HMAC lookup; alias,
  e.g. "Төв дэлгүүр").
- RecipientGroup ↔ BankAccount: many-to-many.
- Member belongs to groups. A Member has: role (owner/admin/cashier/viewer),
  a versioned Consent, an optional PIN, an optional TelegramChat, and an
  optional duty schedule.
- Device (browser/PWA now, desktop agent later) belongs to the tenant and is
  assigned to groups. Each device has a `kind` field (web | agent).
- Delivery happens only if ALL hold: the account is active; the group is
  active; the device/member is paired or verified; the member has active
  consent; the member is on duty.

## 3. Hard security invariants — never violate; write an automated test for each

1. NO MAILBOX ACCESS. Never implement IMAP, POP3, Gmail API, Microsoft Graph
   or any mail OAuth — not even as an optional feature. Emails arrive only via
   a forwarding filter the user sets up, sent to a per-tenant secret inbound
   address (<random 16+ chars>@in.<DOMAIN></domain>).
2. Process an email only if ALL checks pass, in this order:
   a. Envelope recipient = an active tenant inbound address.
   b. Original From is in the allowlist: [PLACEHOLDER Khan Bank sender].
   c. DKIM of the original verifies with d=[PLACEHOLDER], OR the ARC chain
   from google.com verifies and records dkim=pass for that domain.
   d. No attachments; size ≤ 256 KB.
   e. Subject matches [PLACEHOLDER] and the body matches the strict template.
   f. The Кт/To account ∈ tenant accounts AND the Дт/From account ∉ tenant
   accounts (own transfers skipped by default).
   g. Дт amount == Кт amount, same currency, amount > 0.
   Any failure → drop. Log only: tenant_id, reason_code, sha256(Message-ID).
3. Deny-list (OTP, нэг удаагийн, код, нууц үг, password, verification,
   баталгаажуулах, PIN, нэвтрэх, login) is checked on the subject and on the
   body EXCLUDING the payer-written description.
4. NEVER extract, store, show or send: balance, payer name, payer account.
   Persist only: amount, currency, txn datetime, description, to_account
   HMAC, journal_no HMAC.
5. Never store or log the raw email. Process it in memory only.
6. The description is untrusted payer input:
   - Plain text only everywhere; escape it in the UI.
   - Strip control, zero-width and bidi characters.
   - Truncate to 200 chars.
7. No LLM in the processing path. No link following.
8. Idempotency key = HMAC(tenant_id + journal_no + system_date + to_account);
   exactly-once delivery per device/channel.
9. Strict tenant isolation, proven by tests.
10. Consent: no delivery to any member without an active, versioned consent
    record. Revocation is immediate.

## 4. Email template (FAKE sample — never commit real emails)

The real email is probably an HTML table; normalize it to text by
cells/rows first.

    Огноо/Date:2026.10.15 08:37:22
    Шилжүүлгийн мэдээлэл/Transaction information
    Журналын/Journal No:1234567
    Системийн огноо/System Date:2026.10.15 08:37:22
    Дт/From  IBAN/Дансны/Картын дугаар IBAN/Account/Card number  Нэр/Name  Дүн/Amount  Ханш/Rate
    1111111111  B******H TESTPAYER  2,800.00  MNT  1.00
    / Хоёр мянга найман зуун төгрөг /
    Kт/To  Банкны дугаар/Branch No  Банкны нэр/Bank Name  2,800.00  MNT  1.00
    05  Khan Bank - Хаан банк
    IBAN/Дансны/Картын дугаар IBAN/Account/Card Number  Нэр/Name
    2222222222  М*****Т TESTSELLER
    Гүйлгээний утга/Transaction description:
    Худалдан авалт
    Харилцагч танд баярлалаа./Thank you to our customers
    ... (footer — ignore)

Parsing rules:

Normalize Latin K / Cyrillic К in "Кт".

Parse sections in a fixed, anchored order.

1. The description is captured LAST (strictly between its label and
   "Харилцагч танд баярлалаа"), so injected label-like text can't affect other
   fields.

Amounts: Decimal. Datetime: "%Y.%m.%d %H:%M:%S", timezone
Asia/Ulaanbaatar.

Versioned parser (khan_v1). If sender and DKIM are valid but the template
doesn't match → alert the owner "Имэйлийн формат өөрчлөгдсөн байж
магадгүй" with no content.

Tests (fake data only): income; expense; own transfer; multi-account
routing; interbank incoming [PLACEHOLDER]; OTP; spoofed From; DKIM fail;
attachment; oversized; amount mismatch; malicious/huge/zero-width/bidi
description; "код" inside the description; duplicates; Latin vs Cyrillic
"Кт". Plus hypothesis fuzzing of the parser.

## 5. Ingestion

- aiosmtpd on port 25 for MX in.<DOMAIN></domain>: STARTTLS, reject unknown RCPT,
  never relay, per-IP rate limit.
- The Gmail forwarding confirmation mail (forwarding-noreply@google.com) is
  shown only to the owner in the admin panel and expires after 24h.
- If [PLACEHOLDER port 25 blocked]: Cloudflare Email Routing → Worker →
  HTTPS POST with HMAC signature + timestamp (replay protection), feeding the
  same pipeline.
- Health check: if no valid bank email arrives for N business hours
  (configurable), alert the owner that forwarding may be broken.

## 6. Delivery channels

### 6.1 Cashier web view / PWA (PRIMARY in Phase 1)

- Installable PWA. Live updates over SSE (heartbeat every 15s, auto-reconnect
  with backoff). After a reconnect, fetch items missed during the active
  shift.
- When a new transaction arrives:
  1. Show an in-page toast (spec §8.2) + highlight it in the list.
  2. Play a short neutral chime.
  3. If the tab or window is not focused, also show a system notification
     via Web Push (VAPID, pywebpush, service worker). Clicking it focuses the
     cashier page.
  4. Flash the document title ("(1) Орлого 2,800 ₮") until the page gets
     focus.
- Browser autoplay rule: sound needs a user gesture. Make "Ээлж эхлүүлэх"
  (start shift) the gesture that unlocks audio, and show a clear "Дуу
  идэвхгүй — дарж идэвхжүүлнэ үү" bar if audio is still locked.
- Optional Mongolian voice readout (tenant setting, default off), built from
  pre-recorded word clips (number words + "төгрөг орлоо"). Generate the number
  words ourselves; never use the email's amount-in-words. Per-tenant amount
  threshold above which only the chime plays (privacy). Mute toggle.
- Device pairing: an admin creates a one-time pairing code (10 min, single
  use, ≥128-bit, QR + short code). The browser gets a device-bound credential
  (httpOnly, Secure, SameSite=Strict cookie). Admins can revoke a device
  instantly, which closes its SSE stream.
- Shared PC: members start and end shifts with a personal PIN (argon2id,
  rate limit, lockout). Auto-end the shift after N hours of inactivity or at
  a scheduled time. Ending a shift unsubscribes Web Push for that member.
- Known limitation (document it in README, Mongolian): Windows 11 hides OS
  notification banners while another app is full-screen. The in-page view
  and sound still work. Recommended setup: keep the cashier page in a narrow
  side window (compact layout §8.3), and in Windows Settings → System →
  Notifications turn off "When using an app in full-screen mode" and add
  Chrome/Edge to priority notifications. Phase 2's agent removes this
  limitation.

### 6.2 Telegram (optional, owners)

- Webhook with secret_token, privacy mode on, no parse_mode, link previews
  off.
- One-time code linking + consent button. /stop revokes consent.
- Per-member duty schedule.
- The bot leaves any chat it was added to without a valid code.

### 6.3 Notifier interface

Keep a Notifier interface so Viber/SMS can be added later (not implemented
now).

### 6.4 Phase 2 readiness (design now, build later)

- The cashier UI is a standalone frontend bundle that talks to the server
  only through a documented, versioned JSON API + SSE/WebSocket. No
  server-rendered coupling, so a Tauri shell can load it unchanged.
- Device auth and pairing are channel-agnostic (`kind` = web | agent).
- Phase 2 notes (not now): Tauri v2 tray app with its own always-on-top,
  non-activating overlay window instead of OS toasts; outbound-only wss;
  credentials stored in Windows Credential Manager; strict Tauri
  capabilities; signed auto-updates.

### 6.5 On-duty rules

Deliver only to on-duty members/devices. Off duty → nothing is sent and
nothing is queued for them.

## 7. Notification content (identical on all channels, no emoji)

    Орлого
    2,800 ₮
    Утга: Худалдан авалт
    2026.10.15 08:37
    Төв дэлгүүр            (only if the tenant has >1 account)
Format rules:

- Amounts use the "1,234,567 ₮" format; show decimals only if non-zero.
- Other currencies are shown with their ISO code.

## 8. UI/UX spec

### 8.1 Design principles

- Calm, formal, ledger-like. It must NOT look AI-generated: no emoji, no
  gradients, no glassmorphism, no glow, no oversized rounded cards, no
  decorative illustrations, no "✨" style copy.
- One neutral palette plus a single accent (deep green #1F5E3B or navy
  #1D3557 — make it a tenant setting). Use the semantic colors (success,
  warning, error) only for state, never as decoration.
- Typography: IBM Plex Sans (full Cyrillic), self-hosted. Use tabular
  numerals for every number. Amounts are right-aligned in tables.
  Type scale: 13/14/16/20/28/48.
- Borders (1px) instead of shadows. Radius 6px max. 8px spacing grid.
- WCAG 2.2 AA contrast. Full keyboard support, visible focus rings,
  minimum 14px body text.
- Light theme by default (bright shops); dark theme optional.
- Microcopy: formal Mongolian ("Та"), short, no exclamation marks.
  Every empty state and error explains what to do next.
- Never show spinners longer than 300ms without text. Use skeletons for
  lists.

### 8.2 In-page toast

- 360×112px, bottom-right with a 16px margin, white background, 1px border,
  4px accent bar on the left.
- Content:
  - Line 1: "Орлого", 13px, muted.
  - Line 2: the amount, 28px semibold, tabular.
  - Line 3: the description, single line with ellipsis.
  - Bottom right: time "08:37", muted. Bottom left: account alias, if any.
- Auto-dismiss after 8s (configurable 5–20s); hover pauses the timer.
  A thin progress line at the bottom shows the remaining time.
- Max 3 stacked; extra items collapse into "+N бусад".
- Respect prefers-reduced-motion. Entry is a 150ms slide/fade; no bounce.

### 8.3 Cashier view ("User" section) — only this, nothing else

- Top bar:
  - Shop / account alias (left).
  - Connection state (center): "Холбогдсон" in green text, or a full-width
    red bar "Холболт тасарсан — орлогын мэдэгдэл ирэхгүй байж болзошгүй" when
    disconnected for more than 5s.
  - On-duty member name, clock, and an "Ээлж дуусгах" button (right).
- Hero row — the latest transaction:
  - Amount at 48px.
  - Description and time below it.
  - Relative time ("2 минутын өмнө").
  - A new item highlights with a subtle accent background for 10s.
- List — the last 10 transactions only. Columns: Цаг | Дүн | Утга
  (+ Данс if >1 account). The amount column is right-aligned and tabular;
  the description truncates with the full text in a tooltip.
- Verify box: "Дүнгээр шалгах" input. As the cashier types an amount, show
  any match from the last 15 minutes:
  - match: "Орсон байна — 08:37, Худалдан авалт";
  - no match: "Сүүлийн 15 минутад ийм дүнтэй орлого ороогүй байна".
- Optional (tenant setting, default off): today's count and total in the
  footer.
- No navigation, no settings, no history beyond 10 items, no export.
- Layouts:
  - Full layout at ≥1024px.
  - Compact layout at 360–480px wide: hero + list stacked, verify box at the
    top, so it can sit docked beside the POS program.
  - Phone layout for the PWA.
  - Large hit targets (≥40px).

### 8.4 Admin panel (web, desktop-first, responsive)

- Left sidebar navigation:
  - Тойм
  - Дансууд
  - Бүлгүүд
  - Хэрэглэгчид
  - Төхөөрөмжүүд
  - Имэйл холболт
  - Гүйлгээний түүх
  - Тохиргоо
  - Аудит лог
- Тойм:
  - Health cards (bank email last received, devices online, failed
    deliveries).
  - Today's count/total.
  - Recent problems with "what to do" links.
- Имэйл холболт: the inbound address with copy and rotate buttons, plus a
  step-by-step Gmail filter wizard (with screenshots placeholders), a
  "Тест илгээх" self-check, and live status.
- Хэрэглэгчид: invite, consent status, duty schedule, PIN reset, revoke.
- Төхөөрөмжүүд: pair (QR + code), last seen, online state, revoke.
- Гүйлгээний түүх: filters (date, account, amount range), masked, paginated.
  CSV export only for owners/admins, and it is audit-logged.
- Tables: sticky header, zebra rows off, row hover on, right-aligned numbers.
- Every destructive action uses a confirm dialog that names the object.

### 8.5 Design deliverables

- Design tokens (CSS variables) in one file, shared by the admin panel and
  the cashier view (and later the agent).
- A component inventory page (/dev/ui, disabled in production) showing every
  component in every state.
- Playwright screenshot tests for the toast, cashier view (full, compact,
  phone) and key admin pages (light/dark).

## 9. Consent & onboarding

- An admin invites a member (link valid 72h, single use).
- The consent page (Mongolian) states exactly what is shared (amount,
  description, time, account alias), with whom, retention, and how to revoke.
- Store consent_version, accepted_at and the channel.
- The tenant owner accepts the service terms and confirms they may share this
  data with their staff.

## 10. Auth & web security

- Passwords hashed with argon2id. TOTP 2FA mandatory for owners and admins.
- Session cookies: httpOnly, Secure, SameSite=Strict. CSRF protection.
- Strict CSP (no inline scripts), HSTS. No third-party JS/CDN; fonts
  self-hosted.
- Rate limits on auth, pairing and PIN endpoints. Audit log of all admin
  actions.

## 11. Stack & ops

- Server: Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, PostgreSQL, Redis +
  arq, aiosmtpd, pywebpush, pydantic-settings, structlog with a redaction
  filter.
- Admin panel: Jinja + HTMX.
- Cashier view: a small standalone frontend (TypeScript + Vite, minimal
  dependencies, no UI kit that imposes its own look), built as static files
  served by Caddy.
- Docker Compose, Caddy for TLS.
- Field-level encryption (libsodium) for account numbers.
- Containers run as non-root with a read-only filesystem. Expose only ports
  25 and 443. Provide ufw rules and fail2ban.
- Encrypted backups. Transaction retention [PLACEHOLDER, e.g. 90 days], then
  auto-purge.
- CI: ruff, mypy --strict, bandit, pip-audit, npm audit, pytest, Playwright.
  Require ≥95% coverage on the parser, filters, routing, consent, on-duty
  logic and tenant isolation.

## 12. Workflow

1. FIRST write PLAN.md: architecture, data model, STRIDE threat model,
   ASCII wireframes of the cashier view (full + compact) and the main admin
   pages, and open questions. Then STOP and wait for my approval.
2. Write the security invariant tests first, then implement Phase 1 in small
   commits: server core → admin panel → cashier view → Telegram.
3. Do NOT start Phase 2 (desktop agent) until I explicitly approve it.
4. Write README.md covering DNS (MX, SPF), deploy steps, the Gmail filter
   guide (Mongolian), cashier PC setup incl. Windows notification settings
   (Mongolian).
5. Never invent the sender address, subject or DKIM domain. Ask me when
   anything is ambiguous.
6. Never put real account numbers or names in code, tests, fixtures or commits.
