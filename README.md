# active-host-daemon

A small macOS daemon that reports which host is in control by calling Home
Assistant webhooks. The host is `LOCAL` normally and `REMOTE` while a Screen
Sharing client is connected. The initial implementation is intentionally a single
Python file, with detector and notifier boundaries ready for future gesture
detection.

Operating-system inspection is isolated in `SystemState`. `DefaultHostDetector`
applies the host-selection policy to that state, keeping low-level Screen Sharing
detection outside the detector abstraction.

## Setup

Requires Python 3.10+ and macOS.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Edit `config.yaml` with this machine's host name and the webhook URL created in
Home Assistant, then validate and run it:

```sh
python active_host.py --check
python active_host.py
```

The daemon checks for established TCP connections on port 5900 and posts an event
like the following to the configured webhook when the observed host changes:

```json
{
  "host": "macmini",
  "source": "active-host-daemon",
  "timestamp": 1783856401
}
```

The timestamp is generated at delivery time in Unix epoch seconds. The daemon also
reports the initial host at startup. Failed checks and webhook calls are logged and
retried on the next polling interval; the detected host is not committed until the
webhook succeeds.
