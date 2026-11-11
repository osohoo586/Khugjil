# PLAN, Phase 2: Windows desktop agent (Tauri v2)

> Status: **draft for approval.** No Phase 2 code is written until you approve this plan and answer
> the open questions in §9 (or accept the recommended defaults). SPEC §0, §6.1 and §6.4 are the
> source; PLAN.md §1.12 already prepared the server and the cashier bundle for this.

## 0. What the agent is for

On Windows 11, while the POS program is full-screen, the OS hides browser notification banners
(SPEC §6.1). The cashier then sees only the in-page view, which is behind the POS. The agent is a
small tray program that:

- runs the **same cashier UI** (the `cashier/` bundle, unchanged screens) in its own window;
- shows each incoming transaction in its **own always-on-top overlay** that does not take focus from
  the POS, instead of an OS toast;
- connects **outbound only** (HTTPS to `app.khugjil.usukh.shop`); nothing listens on the shop PC;
- keeps its device credential in **Windows Credential Manager**, never in files or web storage;
- runs with **strict Tauri capabilities** and updates itself only with **signed** updates.

Out of scope: macOS/Linux builds, a Windows service, running elevated, any access to the POS
program, screenshots, the clipboard or the file system.

## 1. Architecture

```text
 shop PC (Windows 10/11, per-user install, no admin rights)
 +------------------------------------------------------------------------------+
 |  khugjil-agent.exe (Tauri v2)                                                |
 |                                                                              |
 |  Rust core                                    WebView2 windows (local assets)|
 |  +-----------------------------------+        +---------------------------+  |
 |  | credential store (Credential Mgr) |        | "main": cashier bundle    |  |
 |  | HTTP client (rustls, fixed host)  |<-IPC-->|   (agent build of cashier/)|  |
 |  | SSE client + backoff + refetch    |        +---------------------------+  |
 |  | api_request command (allowlist)   |        | "overlay": 360x112 toast  |  |
 |  | overlay controller (Win32 flags)  |<-IPC-->|   topmost, non-activating |  |
 |  | tray, single instance, autostart  |        +---------------------------+  |
 |  | updater (minisign-verified)       |                                       |
 |  +-----------------+-----------------+                                       |
 +--------------------|---------------------------------------------------------+
                      | HTTPS only, outbound, Authorization: Bearer <device token>
                      v
        app.khugjil.usukh.shop  (Caddy -> api)   /api/v1/*  and  /api/v1/stream (SSE)
```

Key decisions:

1. **The token never reaches JavaScript.** The webviews load only bundled local assets. They call
   the server through one Tauri command, `api_request(method, path, body)`. Rust checks the method
   and path against an allowlist of `/api/v1` endpoints, adds the bearer token and returns the
   JSON. A bug in the UI, or a hostile description that somehow got past the plain-text rendering,
   still cannot read or send the credential anywhere else.
2. **The stream lives in Rust, not in the webview.** Rust holds the SSE connection to
   `/api/v1/stream`, with the same 15 s heartbeat, backoff and `since_seq` refetch the web client
   uses. It forwards each event to the main window and drives the overlay. If the main window is
   hidden or minimized, notifications still work. (This reuses the existing SSE endpoint instead of
   adding a WebSocket one; see P2-Q3.)
3. **The server host is compiled in.** The agent talks only to `https://app.khugjil.usukh.shop`
   (set at build time). A pairing code cannot point it at another server, so a phishing "pairing
   link" cannot attach a cashier PC to an attacker's server.
4. **The same UI code.** The cashier bundle gets a second build mode, `agent`, that swaps three
   injectable adapters: transport (Tauri IPC instead of cookie `fetch`), stream (Tauri events
   instead of `EventSource`) and notifier (overlay instead of in-page toast plus Web Push). The web
   build contains no Tauri code, and the agent build contains no service worker or Web Push code.

### 1.1 Windows

| Window | Behaviour |
|---|---|
| `main` | The cashier view (full, medium or compact layout by width). Normal window; closing it hides it to the tray. Remembers size and position. |
| `overlay` | 360x112 (SPEC §8.2), bottom-right of the primary monitor, 16 px margin. Created hidden at start-up. Always on top, no taskbar button, no activation: `WS_EX_NOACTIVATE \| WS_EX_TOPMOST \| WS_EX_TOOLWINDOW`, shown with `SW_SHOWNOACTIVATE`, so keystrokes keep going to the POS. Up to 3 notes stacked (the window grows upward), the rest collapse into "+N бусад". Auto-hides after the tenant's toast time; hovering pauses it, a click dismisses it and does **not** focus the main window (so a cashier's click never steals the POS's focus by accident). |
| tray | Icon with connection state (connected, disconnected, no shift). Menu: Харах (show the main window), Дуу хаах/нээх (mute), Тухай (version), Гарах (quit, with a confirmation that notifications will stop). |

Limits to verify on real hardware (§7): a POS running in **exclusive** full-screen mode (DirectX
exclusive, rare for POS software) can still cover any window. Borderless full-screen and maximized
windows, which most POS programs use, are covered by the overlay.

### 1.2 Sound and attention

- The chime and the optional voice clips play in the main webview. WebView2 is started with
  `--autoplay-policy=no-user-gesture-required`, so the agent does not need the "click to enable
  sound" banner after a restart.
- Instead of title flashing, the agent flashes the taskbar button (`FlashWindowEx`) until the main
  window is focused.

### 1.3 Start-up and lifecycle

- One instance per Windows user (`tauri-plugin-single-instance`); a second launch shows the
  existing window.
- Optional start at Windows sign-in (`tauri-plugin-autostart`, `HKCU\...\Run`), see P2-Q7.
- Sleep/resume and network changes trigger an immediate reconnect and a `since_seq` refetch.
- Shifts work exactly as on the web: the main window shows the shift-start screen, and a PIN is
  required.

## 2. Server changes

All of these sit behind `KHJ_AGENT_DEVICES_ENABLED`, which stays `false` until the agent ships.

1. **Pairing an agent.** On Төхөөрөмжүүд → Код үүсгэх, the admin chooses the device type:
   "Хөтөч" (web) or "Касс програм" (agent). The pairing code stores that `kind`. A web code
   redeemed by the agent, or an agent code redeemed in a browser, fails with the same error as a
   wrong code.
2. **Redeeming.** `POST /api/v1/pair` with `{"code": …, "client": "agent"}` returns the 256-bit
   device token **in the JSON body** (never in a cookie, never in a URL). It is stored hashed, like
   the web token.
3. **Bearer auth.** `Authorization: Bearer <token>` is accepted only for `kind = agent` devices,
   and the `__Host-khj_dev` cookie only for `kind = web` devices. A token presented the other way
   is refused. Requests with a bearer token must carry **no** `Origin` header: browsers attach one
   to every cross-origin request and every POST, and the agent's Rust client never sends one, so a
   web page cannot replay a leaked agent token through someone's browser.
4. **Token rotation.** As with the web cookie, the token rolls every 30 days. The new value is
   returned once in a response header; the agent stores it in Credential Manager before using it.
   The old token stays valid for 5 minutes so a crash mid-rotation does not lock the device out.
5. **Version gate.** The agent sends `X-Khj-Agent: <version>`. The server answers `426` below a
   configured minimum version, and the agent then shows "Шинэчлэл шаардлагатай" and runs the
   updater. Devices list shows the agent version and its last-seen time.
6. **Eligibility.** `agent_enabled=True` from settings; the `eligible_targets` rules are otherwise
   unchanged. Web Push is never sent to agent devices (they have no subscriptions).
7. **Revocation.** Revoking an agent device deletes its token and closes its stream, exactly like a
   web device. The agent then deletes its stored token and returns to the pairing screen.
8. **Update manifest.** Caddy serves `app.khugjil.usukh.shop/agent/latest.json` and the installer
   files from a read-only directory (P2-Q2). The manifest is public; its integrity comes from the
   minisign signature, not from the host.

## 3. Cashier bundle changes (`cashier/`)

- `src/platform/`: an interface `Platform { transport, stream, notifier, attention }` with two
  implementations, `web.ts` (today's behaviour, moved without changes) and `agent.ts`. `main.ts`
  picks one with a Vite build-time constant, so tree-shaking removes the other.
- `src/overlay.ts` + `overlay.html`: a second entry that renders the existing toast component in
  the overlay window. It gets the transaction from Rust as structured fields and renders them with
  `dom.h()` and `textContent`, like everything else (no `innerHTML`, Trusted Types policy `khj`).
- Agent build: `npm run build:agent` → `cashier/dist-agent/`, which Tauri bundles. The CSP is set
  in `tauri.conf.json`: `default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self';
  img-src 'self'; media-src 'self'; connect-src ipc: http://ipc.localhost; base-uri 'none';
  form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types khj`.
- Dependency: `@tauri-apps/api` (exact version), only in the agent build. The web bundle keeps zero
  runtime dependencies.

## 4. The agent (`agent/`)

```text
agent/
  src-tauri/
    Cargo.toml, Cargo.lock        pinned crates; cargo-deny config
    tauri.conf.json               windows, CSP, bundle (NSIS, per-user), updater pubkey
    capabilities/main.json        main window: api_request, ack, overlay_show, attention only
    capabilities/overlay.json     overlay window: overlay_dismiss, overlay_hover only
    src/main.rs                   setup, tray, single instance, autostart
    src/api.rs                    allowlisted request proxy (method + path patterns, 64 KB body cap)
    src/stream.rs                 SSE client: parser, heartbeat timeout, backoff, refetch
    src/creds.rs                  CredentialStore trait; Windows Credential Manager + in-memory fake
    src/overlay.rs                overlay window, Win32 extended styles, stacking
    src/update.rs                 updater check (start-up + every 6 h), minisign verification
  README.md
```

- **Crates (minimal):** `tauri` 2, `reqwest` (rustls, no native-tls), `keyring` (Windows Credential
  Manager backend), `windows` (only the few Win32 calls for the overlay), `serde`, `tokio`, plus the
  official plugins `single-instance`, `autostart` and `updater`. No `shell`, `fs`, `http`,
  `opener`, `dialog` or `clipboard` plugins.
- **Tauri settings:** `withGlobalTauri: false`; no remote URLs in any window; no
  `dangerousRemoteDomainIpcAccess`; DevTools disabled in release builds; navigation outside the
  bundled assets is blocked (`on_navigation` returns false), and new windows are refused.
- **Credentials:** stored as a generic credential `khugjil/agent/<host>` for the current Windows
  user (protected by DPAPI). Memory holding the token is zeroized after use where practical.
- **Logs:** a small rotating local log with the same allowlist idea as the server (connection
  state, HTTP status codes, version). Never transaction contents, descriptions or tokens. No
  telemetry and no crash reporting to any third party.
- **Installer:** NSIS, `installMode: currentUser` (no admin rights, nothing in `Program Files`),
  WebView2 bootstrapper (P2-Q4). Uninstall removes the stored credential.

## 5. Threat model additions (STRIDE)

New trust boundaries: TB5 agent process ↔ its webviews (IPC), TB6 agent ↔ server (HTTPS, bearer),
TB7 update channel.

| # | Threat | Mitigation | Test |
|---|---|---|---|
| S5 | Attacker pairs a PC to a fake server with a crafted code or link | Host compiled in; pairing input is a code, not a URL | Rust unit test: any URL-shaped input is rejected |
| S6 | Leaked agent token replayed from a browser | Bearer only for `kind = agent`; requests with `Origin` and a bearer token refused | pytest invariant |
| S7 | Web token used as a bearer token, or the other way round | Token lookup filtered by `kind` | pytest invariant |
| T4 | Tampered update | minisign signature checked against the compiled-in public key; HTTPS; only newer versions install | Rust test with a wrong-signature manifest |
| T5 | Hostile description in the overlay | Same plain-text rendering, Trusted Types and CSP as the web; overlay capability has no network access | vitest + Playwright for the overlay entry |
| R3 | Who paired or revoked an agent | Audit log rows include `kind` and agent version | pytest |
| I5 | Token read from disk or web storage | Only in Credential Manager (DPAPI, per user); never in JS, files, logs or URLs | Rust test: token absent from logs and IPC responses |
| I6 | UI bug used to call other endpoints with the token | `api_request` allowlist (methods + path patterns); webview has no other network access (`connect-src` is IPC only) | Rust unit tests per pattern, including traversal and encoded slashes |
| I7 | Notification contents on a locked or shared screen | Overlay only while a shift is open; hidden when the shift ends or the device is revoked | Rust + manual |
| D3 | Notification flood covers the POS | At most 3 stacked, "+N бусад", click to dismiss; never steals focus | manual (§7) |
| E3 | Agent used for privilege escalation | Per-user install, no service, no elevation, no shell/fs plugins; strict capabilities | `tauri.conf.json` and capability files checked by a CI script |
| E4 | Malware running as the same Windows user | Out of scope (it can read anything that user can). Mitigation is detection: revocation in the admin panel takes effect immediately | documented |

Supply chain: `Cargo.lock` committed; `cargo audit` and `cargo deny` (licences, duplicate and
yanked crates, advisories) in CI; plugin versions pinned; the updater signing key is generated and
kept by you (§8).

## 6. Wireframes

Overlay (360x112, bottom-right, accent bar on the left, progress line at the bottom):

```text
+--+---------------------------------------------+
|▌ | Орлого                                      |
|▌ | 2,800 ₮                                     |
|▌ | Худалдан авалт                              |
|▌ | 08:37                          Төв дэлгүүр  |
|▌ |_____________________________________________|  <- 2 px progress line
+--+---------------------------------------------+
```

Several at once (newest at the bottom, the rest collapse):

```text
                           +---------------+
                           | +2 бусад      |
+--------------------------+---------------+
|▌ Орлого  15,000 ₮  ...                    |
+-------------------------------------------+
|▌ Орлого  2,800 ₮   ...                    |
+-------------------------------------------+
```

First run (main window, before pairing):

```text
+----------------------------------------------------------+
| Хугжил касс                                              |
|                                                          |
| Энэ компьютерийг холбох                                  |
| Админ хэсгийн Төхөөрөмжүүд хуудаснаас "Касс програм"     |
| төрлийн код үүсгээд энд оруулна уу.                      |
|                                                          |
| Код  [ XXXX-XXXX-XXXX        ]                           |
|                                                          |
| [ Холбох ]                                               |
|                                                          |
| Сервер: app.khugjil.usukh.shop        Хувилбар 0.1.0     |
+----------------------------------------------------------+
```

Tray menu:

```text
+------------------------+
| ● Холбогдсон           |
|------------------------|
| Харах                  |
| Дуу хаах               |
| Тухай                  |
|------------------------|
| Гарах                  |
+------------------------+
```

Admin, Төхөөрөмжүүд → Код үүсгэх (one new field):

```text
| Төрөл   (•) Хөтөч (Chrome/Edge, утас)   ( ) Касс програм (Windows) |
```

## 7. Build, test and release

- **Local (this Mac):** Rust via `rustup` (needs your OK to install, P2-Q12). Rust unit tests,
  `cargo clippy -D warnings`, and `tauri dev` for UI work run here. The Windows-only overlay code is
  behind `cfg(windows)` with a stub elsewhere, so the rest compiles and tests on macOS.
- **CI (GitHub Actions, `windows-latest`):** build the agent bundle of `cashier/`, `cargo fmt
  --check`, `clippy`, `cargo test`, `cargo audit`, `cargo deny`, then `tauri build` (NSIS). On a
  version tag, the release job signs the update with the minisign key from GitHub secrets and
  produces `latest.json` plus the installer as build artifacts.
- **Server and cashier:** new invariant tests (S6, S7, revocation of agent tokens, tenant isolation
  for bearer auth) are written **first**, as in Phase 1. vitest covers the platform adapters with a
  mocked IPC layer; Playwright gets screenshots of the overlay entry (light only, like the cashier).
- **Manual acceptance on a real Windows PC (you, about 30 minutes; I will write the checklist):**
  POS full-screen (borderless and maximized), focus stays in the POS while notes arrive, two
  monitors, 125 % and 150 % scaling, sleep/resume, network cable out and back, device revoked from
  the admin panel, shift end, update from version N to N+1, uninstall removes the credential.

## 8. Commit order (after approval)

1. `test(agent)`: invariant tests for bearer auth, kind binding, Origin refusal, revocation,
   isolation (xfail).
2. `feat(server)`: agent pairing kind, bearer auth, token rotation, version gate, admin UI, audit.
3. `refactor(cashier)`: platform adapters; the web build unchanged (screenshots must not change).
4. `feat(cashier)`: agent build mode and overlay entry.
5. `feat(agent)`: Tauri scaffold, capabilities, CSP, credential store, request proxy, SSE client,
   tray, single instance.
6. `feat(agent)`: overlay window (Win32 flags), attention, sound policy.
7. `feat(agent)`: updater, Windows CI job, release workflow.
8. `docs`: Mongolian install guide for the agent in README; the Windows 11 full-screen note then
   points to the agent as the fix.

The updater keypair is created by you on your own machine (`npx tauri signer generate`). The
private key and its password go into GitHub secrets that you add yourself. Only the public key is
committed. I never see the private key.

## 9. Open questions

Recommended defaults are marked; answer "P2: defaults OK" to accept all of them.

- **P2-Q1. Windows code signing.** Without an Authenticode signature, Windows SmartScreen warns
  "Windows protected your PC" on first install. Options:
  (a) unsigned, with a Mongolian guide for "More info → Run anyway" (free, **default**);
  (b) SignPath Foundation (free for open-source projects; the GitHub repo is public, but they
  review and approve projects);
  (c) a paid certificate or Azure Trusted Signing (about USD 10/month; conflicts with "free to run").
  The auto-updates are signed with minisign in every option, so only the first install is affected.
- **P2-Q2. Where updates are hosted.** (a) Caddy on `app.khugjil.usukh.shop/agent/` (**default**,
  self-hosted, no third party); (b) GitHub Releases of the public repo.
- **P2-Q3. Stream transport.** SPEC §6.4 says "outbound-only wss". The plan reuses the existing SSE
  endpoint from Rust instead: it is also outbound-only over TLS, already has heartbeats, revocation
  and tests, and avoids a second stream implementation. (a) SSE (**default**); (b) add a WebSocket
  endpoint as the SPEC note says.
- **P2-Q4. Windows versions and WebView2.** Windows 10 22H2 and Windows 11 (**default**). Windows 10
  has been out of Microsoft support since October 2025, but many shop PCs still run it. WebView2
  ships with Windows 11 and most Windows 10 PCs; if it is missing, the installer downloads it from
  Microsoft (default) or the installer embeds it (+~130 MB, works offline).
- **P2-Q5. One PC, two devices?** Can a PC have both the agent and a browser cashier page paired?
  Default: allowed, but the admin page warns that the same cashier would then get every
  notification twice.
- **P2-Q6. Overlay placement.** Default: bottom-right of the primary monitor, 16 px margin. Should
  the owner be able to choose a corner or a monitor per device?
- **P2-Q7. Autostart.** Start the agent at Windows sign-in by default? Default: yes, with a tray
  option to turn it off.
- **P2-Q8. Quitting.** Default: Гарах asks for confirmation only. Alternative: require an
  owner/admin PIN to quit, so a cashier cannot silence notifications.
- **P2-Q9. Sound without a click.** Default: the agent plays the chime without the "click to enable
  sound" step (autoplay allowed for its own local pages only). OK?
- **P2-Q10. TLS trust.** Some antivirus programs intercept HTTPS and install their own root
  certificate. Default: trust only the standard public roots (webpki), so such interception fails
  closed and the tray shows "Холболт хамгаалалтгүй" (stricter). Alternative: trust the Windows
  certificate store (works behind such antivirus, but then the antivirus can read the traffic).
- **P2-Q11. Version policy.** Default: the server refuses agents more than one minor version
  behind once a security fix ships; otherwise any version is accepted.
- **P2-Q12. Toolchain.** May I install Rust with `rustup` (Homebrew) on this Mac for local builds
  and tests? Windows installers are built only in GitHub Actions.

## 10. What does not change

- The SPEC §3 invariants, the email pipeline and the Telegram channel.
- The web cashier and its tests: the agent is an additional `kind`, not a replacement.
- `SPEC.md` wins over this plan if they ever disagree.
