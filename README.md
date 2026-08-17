# DocnGPU — DigitalOcean GPU droplet availability monitor

Tracks, per GPU size and region, whether DigitalOcean GPU droplets are currently
available to create — and logs it over time so you can see availability patterns.

**How it works:** it polls DO's internal `dropletOptions` GraphQL query (the one
the "Create GPU droplet" page uses). Each GPU size has a `region_ids` array
listing regions where it can currently be created; empty = sold out everywhere
(grayed out in the UI). Results are appended to `availability.csv`.

## Architecture (runs 24/7, free, fully unattended)

- **Collector** — `.github/workflows/poll.yml` runs `gpu_monitor.py` half-hourly
  on GitHub Actions and commits new rows to `availability.csv`. No laptop needed.
- **Session renewal** — DO sessions have an **absolute ~12h TTL** (no activity
  extends them; only a real password login mints a new one — established
  empirically 2026-08-17). `.github/workflows/cookie-heal.yml` therefore runs
  `cookie_heal.py` every 8 hours: headless Chromium performs the login (the
  remember-me cookie yields DO's password-only "welcome back" form), validates
  the fresh session against GraphQL, and writes it back to the secrets. The
  session is always renewed before it can die.
- **Self-heal backup** — if a poll still hits a 401, `poll.yml` runs the same
  heal inline and re-polls in the same run (~3 min data gap at worst).
- **Dashboard** — `dashboard.py` (Streamlit) hosted free on Streamlit Community
  Cloud, reading `availability.csv` from this repo. Refreshes on each commit.
- **Auth** — GitHub Actions secrets (never committed; logs print cookie names
  only, never values — this repo is public): `DO_COOKIE` (current Cookie
  header, used by polls), `DO_STATE` (full cookie jar for the browser),
  `DO_EMAIL`/`DO_PASSWORD` (login credentials for renewal), `GH_PAT`
  (fine-grained PAT, Actions-secrets write, so workflows can update secrets).

## Run locally

```bash
python3 gpu_monitor.py                 # one poll → append to availability.csv
python3 gpu_monitor.py --interval 900  # poll every 15 min until Ctrl-C
python3 gpu_monitor.py --print-only    # poll once, don't write CSV
```

The poller is stdlib-only. Cookie is read from `$DO_COOKIE` if set, else from
`secrets.env` (`COOKIE=<full cookie header>`).

Dashboard locally:

```bash
./.venv/bin/streamlit run dashboard.py   # http://localhost:8501
```

## Refreshing the cookie (normally automatic)

Session renewal is handled by the scheduled `cookie-heal.yml` workflow — no
manual step in normal operation. Manual fallbacks, in order of preference:

1. **Trigger the heal:** Actions → cookie-heal → Run workflow (`force: true`
   for a from-scratch login).
2. **Local re-bootstrap** (e.g. after a password change or if the heal's login
   starts failing): `./.venv/bin/python refresh_cookie.py --login` — opens
   Chrome, auto-submits via saved-password autofill, validates, and pushes
   both `DO_COOKIE` and `DO_STATE`.
3. **Last resort, by hand:** DevTools → Network → the `dropletOptions` request
   → copy the `cookie` request header into the `DO_COOKIE` secret and
   `secrets.env` (`COOKIE=...`).

## Data — `availability.csv`

One row per (GPU size, region) per poll:

`ts, size_name, gpu_model, gpu_count, vram_gib, price_per_hour, region_id,
region_slug, region_name, available`  (`available` = 1/0)

## Notes / gotchas

- **Safelisted query:** DO's GraphQL gateway only accepts the exact registered
  query text. `gpu_monitor.py`'s query must stay byte-identical to what the UI
  sends, or you get `PERSISTED_QUERY_NOT_FOUND`. If DO changes their UI, re-copy
  the request body from DevTools.
- **`restriction` field** ("open a ticket to increase your account tier") is an
  account-tier flag, separate from regional availability. We track `region_ids`,
  which reflects DO capacity regardless of account tier.
- **GitHub IP / Cloudflare:** proven a non-issue in practice — both the raw
  GraphQL polls and headless-Chromium page loads/logins run clean from GitHub's
  runner IPs with no Cloudflare challenge (tested repeatedly 2026-08). If that
  ever changes, fallbacks are an Oracle always-free VM or a small DO droplet
  running the same scripts.
- **Session lifetime:** absolute ~12h TTL, server-side, invisible client-side
  (the session cookie never rotates and carries no expiry). Keep-alive page
  loads do NOT extend it — don't rebuild that experiment; it's how the ~12h
  figure was established.
