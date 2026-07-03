#!/bin/bash
# Push the cookie currently in secrets.env up to the GitHub Actions DO_COOKIE
# secret, so the hourly poller uses the fresh session.
#
# Usage:
#   1. Paste the new full cookie after COOKIE= in secrets.env
#   2. ./refresh_cookie.sh
#
# (Optionally pass --run to also kick off a poll immediately.)
set -e
cd "$(dirname "$0")"

if [ ! -f secrets.env ]; then echo "secrets.env not found"; exit 1; fi

# Quick local sanity check that the cookie actually works before pushing it.
echo "Testing cookie locally..."
python3 gpu_monitor.py --print-only

python3 -c "print([l.split('=',1)[1].strip() for l in open('secrets.env') if l.startswith('COOKIE=')][0], end='')" \
  | gh secret set DO_COOKIE
echo "✓ DO_COOKIE secret updated from secrets.env"

if [ "$1" = "--run" ]; then
  gh workflow run poll.yml && echo "✓ triggered a poll run"
fi
