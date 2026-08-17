#!/usr/bin/env python3
"""CI self-heal for the DO session (runs on GitHub Actions).

When the poller's session dies, this script re-mints it the same way a real
browser would: load the GPU page in headless Chromium with the saved cookie
jar (DO_STATE secret); if DO bounces to /login, the remember-me cookie
triggers an automatic re-auth in the login SPA, which issues a fresh session.
The refreshed jar is then validated against the GraphQL endpoint and written
back to the DO_COOKIE and DO_STATE repo secrets so every later run uses it.

Environment:
  DO_STATE    (required) JSON `cookies` array of a Playwright storage_state
  GH_PAT      (required) fine-grained PAT, Actions-secrets read/write on this
              repo. Required up front because a re-auth can rotate the
              remember-me token — losing the rotated token would strand the
              whole pipeline.
  COOKIE_OUT  (optional) path to write the fresh Cookie header, so the calling
              workflow can re-poll immediately without re-reading the secret
  FORCE       (optional) "1" drops the session cookies before launching, to
              force the remember-me re-auth path (for end-to-end testing)

Prints cookie NAMES, statuses, and counts only — never values. This repo is
public, so Actions logs are public.
"""

import json
import os
import subprocess
import sys

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import gpu_monitor

GPU_PAGE = "https://cloud.digitalocean.com/gpus/new"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
COOKIE_FIELDS = {"name", "value", "domain", "path", "expires", "httpOnly",
                 "secure", "sameSite"}
# Dropped in --force mode to simulate a dead session (short-lived / session-
# scoped cookies; the remember-me token is what re-auth actually needs).
SESSION_SCOPED = {"_digitalocean2_session_v4", "sessionID", "TAsessionID",
                  "__cf_bm", "__stripe_sid"}


def gh_secret_set(name, value):
    env = dict(os.environ, GH_TOKEN=os.environ["GH_PAT"])
    subprocess.run(
        ["gh", "secret", "set", name, "-R", os.environ["GITHUB_REPOSITORY"]],
        input=value, text=True, check=True, capture_output=True, env=env,
    )


def main():
    for var in ("DO_STATE", "GH_PAT", "GITHUB_REPOSITORY"):
        if not os.environ.get(var):
            sys.exit(f"{var} env var not set — refusing to run (a re-auth "
                     "could rotate tokens we'd have no way to persist)")

    cookies = json.loads(os.environ["DO_STATE"])
    old_rm = next((c["value"] for c in cookies
                   if c["name"] == "_digitalocean_remember_me"), None)
    if not old_rm:
        sys.exit("No _digitalocean_remember_me in DO_STATE — re-bootstrap: "
                 "run refresh_cookie.py --login locally (it pushes DO_STATE)")

    if os.environ.get("FORCE") == "1":
        cookies = [c for c in cookies if c["name"] not in SESSION_SCOPED]
        print(f"FORCE: dropped session-scoped cookies; {len(cookies)} remain")

    inject = [{k: v for k, v in c.items() if k in COOKIE_FIELDS}
              for c in cookies]
    print(f"injecting {len(inject)} cookies: {sorted(c['name'] for c in inject)}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        ctx.add_cookies(inject)
        page = ctx.new_page()
        page.goto(GPU_PAGE, wait_until="domcontentloaded", timeout=60_000)

        if "/login" in page.url:
            print("bounced to /login — waiting for remember-me re-auth...")
            try:
                page.wait_for_url(
                    lambda u: "cloud.digitalocean.com" in u and "/login" not in u,
                    timeout=45_000,
                )
            except PWTimeout:
                page.screenshot(path="heal_debug.png")
                sys.exit("HEAL FAILED: stuck on the login page — remember-me "
                         "re-auth did not fire. Re-bootstrap: run "
                         "refresh_cookie.py --login locally.")
            print("re-auth fired; reloading the GPU page...")
            page.goto(GPU_PAGE, wait_until="domcontentloaded", timeout=60_000)

        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeout:
            pass  # busy pages never go fully idle

        logged_in = ("cloud.digitalocean.com" in page.url
                     and "/login" not in page.url)
        print(f"landed on: {page.url.split('?')[0]}  logged_in={logged_in}")
        if not logged_in:
            sys.exit("HEAL FAILED: not logged in after page load")

        jar = ctx.cookies(GPU_PAGE)  # cookies applicable to the GraphQL host
        state_cookies = ctx.storage_state()["cookies"]
        browser.close()

    names = sorted({c["name"] for c in jar})
    print(f"jar after heal ({len(jar)}): {names}")
    if "_digitalocean2_session_v4" not in names:
        sys.exit("HEAL FAILED: no session cookie in the refreshed jar")

    new_rm = next((c["value"] for c in state_cookies
                   if c["name"] == "_digitalocean_remember_me"), None)
    print(f"remember_me rotated: {new_rm is not None and new_rm != old_rm}")

    header = "; ".join(f"{c['name']}={c['value']}" for c in jar)
    opts = gpu_monitor.fetch(header)
    n = len([s for s in opts.get("sizes", []) if s.get("gpu_info")])
    print(f"GraphQL validation OK — {n} GPU sizes visible")

    gh_secret_set("DO_COOKIE", header)
    gh_secret_set("DO_STATE", json.dumps(state_cookies))
    print("secrets DO_COOKIE + DO_STATE updated ✓")

    out = os.environ.get("COOKIE_OUT")
    if out:
        with open(out, "w") as fh:
            fh.write(header)
        print(f"fresh cookie header written to {out}")

    print("HEAL SUCCEEDED")


if __name__ == "__main__":
    main()
