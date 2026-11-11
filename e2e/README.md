# Screenshot tests (Playwright)

SPEC §8.5: toast, cashier view (full, compact, phone, shift start) and key admin pages (light
and dark).

```sh
cd e2e
npm ci && npx playwright install chromium
npm test        # builds the cashier, renders admin fixtures, compares screenshots
npm run update  # regenerate baselines after an intended UI change, then review the PNGs
```

- The cashier is tested against its production build (`cashier/dist`) with the JSON API mocked by
  `page.route` and the live stream replaced by a scripted EventSource. Time is frozen.
- Admin pages are rendered from the real Jinja templates with fake data (`render_admin.py`), so
  no database is needed.
- Baselines are platform-specific (fonts render differently per OS). `*-darwin.png` are for local
  runs on macOS; `*-linux.png` are for CI, which runs in `mcr.microsoft.com/playwright:v1.65.0-noble`
  on an arm64 runner (`ubuntu-24.04-arm`). Regenerate the Linux ones in the same image on arm64 (on
  an Apple-silicon Mac, Docker runs it natively; amd64 emulation crashes Chromium):
  ```sh
  npm run fixtures   # builds the cashier bundles and renders the admin pages on the host
  docker run --rm --platform linux/arm64 --user "$(id -u):$(id -g)" -e HOME=/tmp -e CI=1 \
    -v "$PWD/..:/work" -w /work/e2e mcr.microsoft.com/playwright:v1.65.0-noble \
    npx playwright test --update-snapshots
  ```
  Review the PNGs before committing. When upgrading Playwright, change the image tag in both
  places (here and `.github/workflows/ci.yml`).

## Browser flows (`flows/`)

`npm run flows` (or `make e2e-flows`) drives a real browser through the full local stack started
by `scripts/dev-up.sh`: the owner invite with TOTP enrolment, sign-in, pairing a cashier PC with a
code from the admin panel, a PIN shift, a live payment arriving over SSE through Caddy, and
revoking the PC. Each run creates its own shop (`scripts/dev-seed.sh tenant …`), so the demo shop
is left alone. Any 403 or 5xx response fails the run: these tests exist because a referrer-policy
bug once made every admin form fail in real browsers while in-process tests passed. CI runs them
in the `flows` job.

