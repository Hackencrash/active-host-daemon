# active-host-daemon

A small macOS daemon that reports whether a Screen Sharing client is connected by
calling Home Assistant webhooks. The initial implementation is intentionally a
single Python file, with detector and notifier boundaries ready for future gesture
detection.

## Setup

Requires Python 3.10+ and macOS.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Edit `config.yaml` with the webhook URLs created in Home Assistant, then validate
and run it:

```sh
python active_host.py --check
python active_host.py
```

The daemon checks for established TCP connections on port 5900 and posts an empty
JSON object to the active or inactive webhook when the observed state changes. It
also reports the initial state at startup. Failed checks and webhook calls are
logged and retried on the next polling interval; the state is not committed until
the matching webhook succeeds.
