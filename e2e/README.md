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
- Baselines are platform-specific (fonts render differently per OS). The committed ones are for
  macOS (`*-darwin.png`). For Linux CI, generate `*-linux.png` once inside the official image
  (`mcr.microsoft.com/playwright`) with `npm run update`, review them, and commit.
