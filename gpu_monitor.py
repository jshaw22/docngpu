#!/usr/bin/env python3
"""
DigitalOcean GPU droplet availability monitor.

Polls the same internal GraphQL endpoint the DO "Create GPU droplet" page uses
(operationName: dropletOptions) and records, per GPU size and per region, whether
that size is currently available to create.

Availability signal: each GPU size object has a `region_ids` array listing the
regions where it can currently be created. Empty array => unavailable everywhere
(grayed out in the UI). We cross-reference those ids against the `regions` lookup
table in the same response and write one row per (size, region) per poll.

Data is appended to availability.csv (one row per size+region per poll).
Cookie comes from $DO_COOKIE (GitHub Actions) or secrets.env (local).

Stdlib only — no pip install required. Run:

    python3 gpu_monitor.py            # one poll, append to CSV, print summary
    python3 gpu_monitor.py --interval 900   # poll every 15 min until Ctrl-C
    python3 gpu_monitor.py --once --print-only   # poll once, don't write CSV
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "availability.csv")
SECRETS_PATH = os.path.join(HERE, "secrets.env")

# Column order for availability.csv (matches the tuples parse_rows returns).
COLUMNS = [
    "ts", "size_name", "gpu_model", "gpu_count", "vram_gib",
    "price_per_hour", "region_id", "region_slug", "region_name", "available",
]

GRAPHQL_URL = "https://cloud.digitalocean.com/graphql?i=monitor"

# The exact request body the DO UI sends. DO's GraphQL gateway safelists
# operations (rejects anything but the registered query text with
# PERSISTED_QUERY_NOT_FOUND), so this must stay byte-for-byte identical to the
# browser's request. If DO changes their UI query, re-copy it from DevTools.
GRAPHQL_RAW_BODY = (
    '{"operationName":"dropletOptions","variables":{"dropletOptionsParams":'
    '{"type":"gpus"}},"query":"query dropletOptions($dropletOptionsParams: '
    'ListDropletOptionsRequest) {\\n  dropletOptions(dropletOptionsParams: '
    '$dropletOptionsParams) {\\n    has_admin_images\\n    distributions {\\n'
    '      id\\n      name\\n      is_deprecated\\n      images {\\n        '
    'name\\n        id\\n        disk\\n        region_slug\\n        '
    'distribution_parameterized_name\\n        slug_name\\n        __typename\\n'
    '      }\\n      required_features\\n      __typename\\n    }\\n    sizes {\\n'
    '      name\\n      restriction\\n      disk\\n      disk_in_bytes\\n      '
    'id\\n      size_category {\\n        name\\n        id\\n        __typename\\n'
    '      }\\n      disk_info {\\n        size {\\n          amount\\n          '
    'unit\\n          __typename\\n        }\\n        type\\n        __typename\\n'
    '      }\\n      gpu_info {\\n        vram {\\n          unit\\n          '
    'amount\\n          __typename\\n        }\\n        model\\n        count\\n'
    '        __typename\\n      }\\n      price_per_month\\n      price_per_hour\\n'
    '      cpu_count\\n      bandwidth_in_bytes\\n      memory_in_bytes\\n      '
    'region_ids\\n      backup_prices {\\n        plan\\n        monthly_cost\\n'
    '        __typename\\n      }\\n      __typename\\n    }\\n    initial_state {\\n'
    '      size_id\\n      requires_payment_method\\n      region_id\\n      '
    'image_id\\n      droplet_limit\\n      droplet_count\\n      '
    'default_region_by_category {\\n        region_id\\n        category_id\\n'
    '        __typename\\n      }\\n      __typename\\n    }\\n    regions {\\n'
    '      slug\\n      name\\n      id\\n      is_default\\n      features\\n'
    '      __typename\\n    }\\n    __typename\\n  }\\n}\\n"}'
)


def load_cookie(path=SECRETS_PATH):
    """Cookie from $DO_COOKIE (used in GitHub Actions) if set, else COOKIE=...
    from secrets.env. The value may contain '=' and ';'."""
    env = os.environ.get("DO_COOKIE")
    if env and env.strip():
        return env.strip()
    if not os.path.exists(path):
        sys.exit(
            f"Missing {path} and no $DO_COOKIE set.\n"
            "Create secrets.env with a single line:\n"
            "  COOKIE=<paste the full Cookie header from the browser>\n"
        )
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                if key.strip() == "COOKIE":
                    return val.strip()
    sys.exit(f"No COOKIE=... line found in {path}")


def fetch(cookie):
    """POST the GraphQL query, return parsed JSON. Raise with a clear message
    if the session cookie looks expired/invalid."""
    data = GRAPHQL_RAW_BODY.encode("utf-8")
    req = urllib.request.Request(GRAPHQL_URL, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "*/*")
    req.add_header("origin", "https://cloud.digitalocean.com")
    req.add_header("referer", "https://cloud.digitalocean.com/gpus/new")
    req.add_header("apollographql-client-name", "ui-droplets")
    req.add_header(
        "user-agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    )
    req.add_header("cookie", cookie)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise SystemExit(
            f"HTTP {e.code} from DO GraphQL. The session cookie has likely "
            f"expired — refresh it in secrets.env.\nResponse start: {body}"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(
            "Response was not JSON (got HTML?). The session cookie has likely "
            "expired — re-copy the Cookie header from the browser into secrets.env.\n"
            f"Response start: {raw[:300]}"
        )

    if "errors" in payload and payload["errors"]:
        raise SystemExit("GraphQL errors: " + json.dumps(payload["errors"])[:500])

    opts = payload.get("data", {}).get("dropletOptions")
    if not opts:
        raise SystemExit("Unexpected payload shape: " + raw[:300])
    return opts


def parse_rows(opts, ts):
    """Flatten to one row per (GPU size, region) with available 0/1."""
    regions = opts.get("regions", [])
    # id may be string; normalize to int for matching against region_ids.
    region_by_id = {int(r["id"]): r for r in regions}

    rows = []
    for size in opts.get("sizes", []):
        gpu = size.get("gpu_info")
        if not gpu:
            continue  # skip non-GPU sizes, if any
        avail_ids = {int(x) for x in (size.get("region_ids") or [])}
        vram = (gpu.get("vram") or {}).get("amount")
        for rid, region in region_by_id.items():
            rows.append(
                (
                    ts,
                    size["name"],
                    gpu.get("model"),
                    gpu.get("count"),
                    vram,
                    size.get("price_per_hour"),
                    rid,
                    region["slug"],
                    region["name"],
                    1 if rid in avail_ids else 0,
                )
            )
    return rows


def write_csv(rows, path=CSV_PATH):
    """Append rows to availability.csv, writing the header if the file is new."""
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(COLUMNS)
        w.writerows(rows)


def write_no_data(ts, path=CSV_PATH):
    """Append a sentinel row marking a failed poll (expired cookie, network
    error, ...) so downstream consumers can tell 'no data' apart from a poll
    that never ran. Every field except ts is empty; size_name is 'NO_DATA'."""
    write_csv([(ts, "NO_DATA", "", "", "", "", "", "", "", "")], path)


def print_summary(rows, ts):
    available = [r for r in rows if r[9] == 1]
    n_sizes = len({r[1] for r in rows})
    print(f"[{ts}] polled {n_sizes} GPU sizes x {len(rows)//max(n_sizes,1)} regions "
          f"= {len(rows)} rows")
    if not available:
        print("  -> ALL SOLD OUT (no GPU size available in any region)")
        return
    print(f"  -> {len(available)} AVAILABLE combos:")
    for r in available:
        # size_name, gpu_count x model in region_name
        print(f"     {r[1]:<22} {r[3]}x {r[2]:<16} in {r[8]} ({r[7]})  "
              f"${r[5]}/hr")


def poll_once(write=True):
    cookie = load_cookie()
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        opts = fetch(cookie)
    except SystemExit as e:
        # Record the failed poll so the gap shows up as "no data" downstream,
        # then re-raise so callers (and CI) still see a non-zero exit.
        if write:
            write_no_data(ts)
            print(f"[{ts}] poll failed — wrote NO_DATA row", file=sys.stderr)
        raise
    rows = parse_rows(opts, ts)
    if write:
        write_csv(rows)
    print_summary(rows, ts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=int, default=0,
                    help="seconds between polls; 0 = run once and exit")
    ap.add_argument("--print-only", action="store_true",
                    help="don't write to the CSV, just print")
    ap.add_argument("--once", action="store_true",
                    help="force a single poll (ignore --interval)")
    args = ap.parse_args()

    write = not args.print_only
    if args.interval and not args.once:
        print(f"Polling every {args.interval}s. Ctrl-C to stop. CSV: {CSV_PATH}")
        try:
            while True:
                try:
                    poll_once(write=write)
                except SystemExit as e:
                    print(f"  poll failed: {e}", file=sys.stderr)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        poll_once(write=write)


if __name__ == "__main__":
    main()
