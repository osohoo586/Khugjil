# Desktop agent (Phase 2)

A Windows tray program (Tauri v2) that runs the cashier view and shows each incoming transaction in
an always-on-top overlay that never takes focus from the POS program. Design: `PLAN-PHASE2.md`.
The Mongolian install guide for shops is in `README.md`.

```text
cashier/ (same screens)  --vite.agent.config.ts-->  cashier/dist-agent/  --bundled by-->  agent/src-tauri/
  platform-agent.ts: IPC transport, Rust-fed stream,     agent.html + overlay.html          Rust core: API proxy,
  overlay notifier, taskbar attention                                                       SSE, overlay, tray, updater
```

## Layout

| Path | What |
|---|---|
| `cashier/src/platform-agent.ts`, `main-agent.ts`, `overlay.ts` | Agent side of the shared cashier UI |
| `agent/src-tauri/src/routes.rs` | Allowlist of the API calls the webview may make |
| `agent/src-tauri/src/api.rs` | HTTPS client: bearer token, `X-Khj-Agent`, token rotation, pairing |
| `agent/src-tauri/src/creds.rs` | Device token in Windows Credential Manager |
| `agent/src-tauri/src/sse.rs`, `stream.rs` | Live stream: SSE parser, heartbeat watchdog, backoff |
| `agent/src-tauri/src/overlay.rs`, `win.rs` | Overlay window: placement, Win32 no-activate/topmost |
| `agent/src-tauri/capabilities/*.json` | What each window may call (checked by `agent/scripts/check-capabilities.mjs`) |

## Develop and test

Rust is installed with `rustup` (Homebrew's `rustup` is keg-only: put `/opt/homebrew/opt/rustup/bin`
on `PATH`).

```sh
make agent-check                         # capability guard, cashier agent build, rustfmt, clippy, cargo test
cd agent && npm ci && npx tauri dev      # runs on macOS too (no Win32 overlay styles there)
cd cashier && npx vitest run src/platform-agent.test.ts
```

Windows-only code (`win.rs`) is compiled, linted and tested in CI on `windows-latest` (job `agent`,
which also builds an unsigned NSIS installer as an artifact). Cross-checking for Windows from macOS
needs the Windows SDK headers (aws-lc), so it is not part of `make agent-check`.

Debug builds print command names and API paths (never bodies or tokens) to stderr.

## Server side

Agents are off until the server has `KHJ_AGENT_DEVICES_ENABLED=true` (see `docs/configuration.md`).
Then the admin panel offers **Төрөл → Агент** on Төхөөрөмжүүд. `KHJ_AGENT_MIN_VERSION` makes the
server answer 426 to older agents, which then run the updater.

## Releases and signed updates

The update signature, not the download host, protects updates. The signing key belongs to the owner:

1. On your own computer: `cd agent && npx tauri signer generate -w ~/.tauri/khugjil-agent.key`.
   Keep the private key and its password offline. Never commit them or paste them anywhere.
2. In GitHub → Settings → Secrets → Actions, add `TAURI_SIGNING_PRIVATE_KEY` (the key file's
   contents) and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.
3. Put the **public** key into `agent/src-tauri/tauri.conf.json` → `plugins.updater.pubkey` and
   commit it. Until then the updater is disabled and the release workflow refuses to run.
4. Bump `version` in `tauri.conf.json` and `Cargo.toml`, then tag `agent-v<version>` and push the tag.
5. Download the workflow artifact (`*-setup.exe`, `*-setup.exe.sig`, `latest.json`) and copy it to
   `deploy/agent-releases/` on the server. Caddy serves it at `https://app.<DOMAIN>/agent/`.

Agents check for updates at start-up and whenever the server answers 426. They start at Windows
sign-in, so a normal update reaches every shop within a day; raise `KHJ_AGENT_MIN_VERSION` to force
it sooner. The installer is not Authenticode-signed (PLAN-PHASE2 P2-Q1 default), so Windows
SmartScreen warns on the first install only.

## Manual acceptance on a real Windows PC

The parts below cannot be tested in CI. Run them once per release candidate on Windows 11 (and
Windows 10 22H2 if shops use it), with the server's agent flag on and a test tenant:

- [ ] Install without admin rights; SmartScreen "Run anyway" works; the app starts in the tray.
- [ ] Pair with an **Агент** code; a **Веб** code is refused with the normal error.
- [ ] Start a shift with a PIN; the chime plays on the first transaction without any click.
- [ ] POS (or Notepad) **maximized** and **borderless full-screen**: the overlay appears bottom-right
      above it, typing keeps going into the POS while it is shown, and a click on the overlay closes
      it without activating anything.
- [ ] Three or more transactions at once: at most 3 stacked, then "+N бусад"; hover pauses.
- [ ] A description such as `<img src=x onerror=alert(1)>` shows as plain text in both windows.
- [ ] Two monitors, and 125 % / 150 % scaling: overlay sits on the primary monitor, not cut off.
- [ ] Unplug the network for a minute: tray shows "Холболт тасарсан", the red bar appears after 5 s;
      reconnect: missed transactions arrive.
- [ ] Sleep and wake the PC: the stream reconnects.
- [ ] Revoke the device in the admin panel: the agent shows the revoked screen within seconds, and
      Credential Manager (Windows Credentials → `khugjil-agent`) no longer holds the entry.
- [ ] End the shift: the overlay clears and no more notes appear.
- [ ] Close the window: the app stays in the tray; **Гарах** asks for confirmation.
- [ ] Update from version N to N+1 through `latest.json`; a tampered `.sig` is refused.
- [ ] Uninstall removes the program. (The credential entry is removed by revoking the device.)

If a window stays blank, check the WebView2 console (debug build) for a Content-Security-Policy or
Trusted Types error first.
