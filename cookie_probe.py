#!/usr/bin/env python3
"""CI probe: can headless Chromium on a GitHub Actions runner hold a working
DigitalOcean session?

Injects the cookie jar from the DO_STATE secret (the `cookies` array of a
Playwright storage_state), loads the GPU-droplet page, and reports whether the
browser lands logged-in or gets bounced/challenged. Then validates the session
cookie against the GraphQL endpoint via gpu_monitor.fetch.

Prints cookie NAMES, statuses, and counts only — never values. This repo is
public, so Actions logs are public.
"""

import json
import os
import sys

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import gpu_monitor

GPU_PAGE = "https://cloud.digitalocean.com/gpus/new"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
# Fields playwright's add_cookies accepts (storage_state carries a superset).
COOKIE_FIELDS = {"name", "value", "domain", "path", "expires", "httpOnly",
                 "secure", "sameSite"}


def main():
    raw = os.environ.get("DO_STATE")
    if not raw:
        sys.exit("DO_STATE env var not set")
    cookies = [{k: v for k, v in c.items() if k in COOKIE_FIELDS}
               for c in json.loads(raw)]
    print(f"injecting {len(cookies)} cookies: {sorted(c['name'] for c in cookies)}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto(GPU_PAGE, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeout:
            pass  # busy pages never go fully idle

        title = page.title()
        print(f"landed on: {page.url.split('?')[0]}")
        print(f"title: {title!r}")
        challenged = "just a moment" in title.lower() or "challenge" in page.url
        logged_in = ("cloud.digitalocean.com" in page.url
                     and "/login" not in page.url)
        print(f"cloudflare challenge: {challenged}")
        print(f"logged in: {logged_in}")

        jar = ctx.cookies(GPU_PAGE)
        print(f"jar after load ({len(jar)}): {sorted({c['name'] for c in jar})}")
        sess = next((c["value"] for c in jar
                     if c["name"] == "_digitalocean2_session_v4"), None)
        browser.close()

    if challenged or not logged_in or not sess:
        sys.exit("PROBE FAILED: no working session in headless Chromium on this runner")

    opts = gpu_monitor.fetch(f"_digitalocean2_session_v4={sess}")
    n = len([s for s in opts.get("sizes", []) if s.get("gpu_info")])
    print(f"GraphQL validation OK — {n} GPU sizes visible")
    print("PROBE PASSED: headless Chromium on this runner holds a working DO session")


if __name__ == "__main__":
    main()
