#!/bin/zsh
# Double-clickable DO cookie refresh. Opens the automated login window
# (logs itself in), updates secrets.env, pushes the DO_COOKIE secret,
# and kicks an immediate poll so the data gap closes right away.
cd /Users/justinshaw/Documents/docngpu || exit 1
./.venv/bin/python refresh_cookie.py --login && \
  /usr/local/bin/gh workflow run poll.yml && \
  echo "✓ poll triggered — data resuming"
echo "(you can close this window)"
