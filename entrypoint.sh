#!/bin/sh
# Headful Firefox needs a real X display. We start Xvfb explicitly (instead of the
# xvfb-run shell wrapper, which failed silently under cap_drop:ALL + tmpfs /tmp and
# exited before exec'ing python — empty logs + dead health endpoint).
set -e

# tmpfs /tmp mounts empty; X needs this socket dir to exist.
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

export DISPLAY=:99

# Single-tenant container: fixed display, no TCP, no xauth needed for a local
# same-uid client. Log Xvfb's own output so failures are visible.
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &

# Wait up to ~5s for the X socket before launching the app.
i=0
while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 50 ]; do
  i=$((i + 1))
  sleep 0.1
done

# exec so python is the main process: stdout → Docker logs, signals forwarded.
exec python -u run.py
