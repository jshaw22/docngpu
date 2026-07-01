# DocnGPU — DigitalOcean GPU droplet availability monitor

Tracks, per GPU size and region, whether DigitalOcean GPU droplets are currently
available to create — and logs it over time so you can see availability patterns.

**How it works:** it polls DO's internal `dropletOptions` GraphQL query (the one
the "Create GPU droplet" page uses). Each GPU size has a `region_ids` array
listing regions where it can currently be created; empty = sold out everywhere
(grayed out in the UI). Results are appended to `availability.csv`.

## Architecture (runs 24/7, free)

- **Collector** — `.github/workflows/poll.yml` runs `gpu_monitor.py` hourly on
  GitHub Actions and commits new rows to `availability.csv`. No laptop needed.
- **Dashboard** — `dashboard.py` (Streamlit) hosted free on Streamlit Community
  Cloud, reading `availability.csv` from this repo. Refreshes on each commit.
- **Auth** — the DO session cookie lives in the GitHub Actions secret
  `DO_COOKIE` (never committed).

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

## Refreshing the cookie

The DO session cookie eventually expires. When polls start failing (HTTP 4xx or
a non-JSON response), re-copy it:

DevTools → Network → the `dropletOptions` request → Headers → Request Headers →
`cookie` → copy the whole value. Then update it in **both** places you use:
- GitHub: repo → Settings → Secrets and variables → Actions → `DO_COOKIE` → update.
- Local: paste after `COOKIE=` in `secrets.env`.

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
- **GitHub IP / Cloudflare:** the endpoint sits behind Cloudflare. Polling from
  GitHub's datacenter IPs *may* get challenged even with a valid cookie. If the
  hourly Action starts failing where local runs succeed, that's the cause — see
  the fallback options in the project history (VPS / home machine / Playwright).
