#!/usr/bin/env python3
"""Automated DO session-cookie refresher.

Keeps the DO_COOKIE GitHub Actions secret fresh so the half-hourly poller
never dies of an expired session. Drives a real Chrome with a dedicated,
persistent profile (~/.docngpu/browser_profile) that stays logged into
DigitalOcean — separate from your daily browser, so nothing rotates the
session out from under us.

One-time setup (opens a Chrome window; log into DO in it):

    ./.venv/bin/python refresh_cookie.py --login

After that, headless refresh (what the launchd job runs every 30 min):

    ./.venv/bin/python refresh_cookie.py

Each refresh: load the GPU-droplet page in the profile (which slides/renews
the session exactly like normal browsing), dump the cookie jar to a Cookie
header, validate it against the GraphQL endpoint via gpu_monitor.fetch, then
write secrets.env and push the GitHub secret. If the profile is ever truly
logged out, headless runs exit non-zero and pop a macOS notification telling
you to re-run --login.

The old refresh_cookie.sh (manual paste -> push) still works as a fallback.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import gpu_monitor

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(HERE, "secrets.env")
PROFILE_DIR = os.path.expanduser("~/.docngpu/browser_profile")
# Chrome discards session-scoped cookies (like _digitalocean2_session_v4) when
# the process exits, so we snapshot the full jar here after each successful
# refresh and re-inject it on the next launch.
STATE_PATH = os.path.expanduser("~/.docngpu/state.json")
GPU_PAGE = "https://cloud.digitalocean.com/gpus/new"
LOGIN_WAIT_S = 600  # how long --login waits for you to finish logging in


def log(msg):
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def notify(text):
    """Best-effort macOS notification (used when a re-login is needed)."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{text}" with title "DO GPU monitor"'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def launch_context(pw, headed):
    """Persistent-profile browser. Prefer the system Chrome (closer to a real
    browser for Cloudflare); fall back to Playwright's bundled Chromium."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    last_err = None
    for channel in ("chrome", None):
        try:
            return pw.chromium.launch_persistent_context(
                PROFILE_DIR, headless=not headed, channel=channel,
            )
        except Exception as e:
            last_err = e
    sys.exit(
        f"Could not launch a browser: {last_err}\n"
        "If the profile is locked, another refresh may be running. Otherwise "
        "install the fallback browser with: ./.venv/bin/playwright install chromium"
    )


def logged_in(page):
    return "cloud.digitalocean.com" in page.url and "/login" not in page.url


def try_prefill_click(page):
    """Chrome prefills saved credentials but hides the values from JS until a
    trusted user gesture, so don't try to read them — click into the form
    (which commits the pending autofill) and press Log In. If the fields were
    actually empty, DO just shows a validation error and we fall back to a
    manual login. Returns True once we've left the login page (never raises)."""
    try:
        if not page.locator("#email").count():
            return False
        page.locator("#email").click(timeout=3_000)
        page.wait_for_timeout(2_000)  # let the autofill commit
        page.click('button[type="submit"]', timeout=5_000)
        page.wait_for_url(
            lambda url: "cloud.digitalocean.com" in url and "/login" not in url,
            timeout=15_000,
        )
        return True
    except Exception:
        return False


def saved_cookies():
    """Cookies to re-inject at launch: last saved browser state, else bootstrap
    from the COOKIE header in secrets.env (no domain info there, so pin them
    to the cloud host)."""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            return json.load(fh).get("cookies", [])
    if os.path.exists(SECRETS_PATH) and not os.environ.get("DO_COOKIE"):
        try:
            header = gpu_monitor.load_cookie()
        except SystemExit:
            return []
        return [
            {"name": k.strip(), "value": v, "url": "https://cloud.digitalocean.com/"}
            for k, v in (p.split("=", 1) for p in header.split(";") if "=" in p)
        ]
    return []


def save_state(ctx):
    state = ctx.storage_state()
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)


def mint_cookie(headed=False):
    """Open the GPU page in the persistent profile and return the current
    Cookie header. In --login (headed) mode, waits for a manual login first."""
    with sync_playwright() as pw:
        ctx = launch_context(pw, headed)
        try:
            try:
                ctx.add_cookies(saved_cookies())
            except Exception as e:
                log(f"could not re-inject saved cookies ({e}); continuing without")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(GPU_PAGE, wait_until="domcontentloaded", timeout=60_000)

            if not logged_in(page):
                if not headed:
                    # Give the login page a chance to auto-re-auth off the
                    # persistent remember-me token before declaring defeat.
                    try:
                        page.wait_for_url(
                            lambda url: "cloud.digitalocean.com" in url
                            and "/login" not in url,
                            timeout=20_000,
                        )
                        page.goto(GPU_PAGE, wait_until="domcontentloaded",
                                  timeout=60_000)
                    except PWTimeout:
                        notify("Cookie refresher is logged out — run refresh_cookie.py --login")
                        sys.exit(
                            "Profile is logged out (landed on the login page). "
                            "Re-run with --login to sign in again."
                        )
                else:
                    if try_prefill_click(page):
                        log("Auto-submitted the prefilled login ✓")
                    else:
                        log("Auto-submit didn't take — log into DigitalOcean in the Chrome window...")
                    # Whether we clicked or the user does, wait for the same
                    # signal: leaving the login page. If the auto-click didn't
                    # take (2FA, captcha, bad fill), just log in manually.
                    page.wait_for_url(
                        lambda url: "cloud.digitalocean.com" in url and "/login" not in url,
                        timeout=LOGIN_WAIT_S * 1_000,
                    )
                    log("Login detected; loading the GPU page to establish the session...")
                    page.goto(GPU_PAGE, wait_until="domcontentloaded", timeout=60_000)

            # Let the SPA finish its authenticated calls — this is also what
            # renews the session server-side, like normal browsing would.
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except PWTimeout:
                pass  # busy pages never go fully idle; the cookies are set by now

            cookies = ctx.cookies(GPU_PAGE)
            if logged_in(page):
                save_state(ctx)  # keep session cookies across browser restarts
        finally:
            ctx.close()

    if not cookies:
        sys.exit("Browser returned no cookies for cloud.digitalocean.com")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def write_secrets(header):
    """Replace the COOKIE= line in secrets.env, keeping any other lines."""
    lines = []
    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH) as fh:
            lines = [l for l in fh.read().splitlines() if not l.startswith("COOKIE=")]
    lines.append("COOKIE=" + header)
    with open(SECRETS_PATH, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def push_secret(header):
    gh = shutil.which("gh") or "/usr/local/bin/gh"
    subprocess.run(
        [gh, "secret", "set", "DO_COOKIE"],
        input=header, text=True, cwd=HERE, check=True, capture_output=True,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--login", action="store_true",
                    help="open a visible Chrome window for a one-time manual login")
    ap.add_argument("--no-push", action="store_true",
                    help="update secrets.env but skip pushing the GitHub secret")
    args = ap.parse_args()

    header = mint_cookie(headed=args.login)

    # Validate against the real endpoint before touching secrets anywhere.
    # gpu_monitor.fetch raises SystemExit with a clear message on failure.
    opts = gpu_monitor.fetch(header)
    n_sizes = len([s for s in opts.get("sizes", []) if s.get("gpu_info")])
    log(f"Cookie validated against GraphQL ({n_sizes} GPU sizes visible)")

    write_secrets(header)
    log(f"secrets.env updated ({len(header)} byte cookie header)")

    if args.no_push:
        log("--no-push: skipping GitHub secret update")
        return
    push_secret(header)
    log("GitHub secret DO_COOKIE updated ✓")


if __name__ == "__main__":
    main()
