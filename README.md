# Active Host Daemon

## Overview

Active Host Daemon is a macOS utility that detects whether local or remote input
is active and reports host changes to Home Assistant. It runs in the background as
a per-user LaunchAgent and uses a Quartz event tap to observe local input.

The transfer gesture is Ctrl plus a deliberate movement to the active display's
left edge. After the configured movement and dwell requirements are satisfied, the
daemon selects the remote host unless Screen Sharing is active. Genuine local input
returns control to the local host.

## Features

- Native macOS Quartz input monitoring
- Explicit local and remote host states
- Multi-display left-edge detection
- Screen Sharing safety check
- Home Assistant webhook notifications on host changes
- Automatic startup and restart through launchd
- Event-tap health watchdog and operational diagnostics
- Install, start, stop, restart, status, and uninstall commands

## Installation

Requirements:

- macOS
- Python 3.10 or newer
- A Home Assistant webhook URL

Clone the repository, create its virtual environment, install dependencies, and
run the installer:

```sh
cd /path/to/active-host-daemon
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./install.sh
```

The installer renders the LaunchAgent with the repository's absolute path, writes
it to `~/Library/LaunchAgents`, creates `~/Library/Logs`, and starts the daemon.

## Configuration

Edit `config.yaml` before installation:

```yaml
hosts:
  local: "macmini"
  remote: "laptop"

poll_interval: 1
screen_sharing_port: 5900

home_assistant:
  webhook: "http://home-assistant.local:8123/api/webhook/active_host"
  request_timeout: 10

logging:
  level: INFO
```

- `hosts.local` is sent when local input is active.
- `hosts.remote` is sent after a successful edge-transfer gesture.
- `poll_interval` controls how frequently the daemon loop observes detector state.
- `screen_sharing_port` is the local Screen Sharing TCP port.
- `home_assistant.webhook` must be an absolute HTTP or HTTPS URL.
- `request_timeout` is the webhook timeout in seconds.
- `logging.level` accepts standard Python logging levels such as `INFO` or `DEBUG`.

Validate configuration without starting the detector:

```sh
.venv/bin/python active_host.py --check
```

Display the installed software version:

```sh
.venv/bin/python active_host.py --version
```

## First Run

After `./install.sh`, inspect the service:

```sh
./status.sh
```

The initial Home Assistant event is sent when the daemon first observes its host
state. Subsequent events are sent only when the detected host changes. Payloads use
this format:

```json
{
  "host": "macmini",
  "source": "active-host-daemon",
  "timestamp": 1783856401
}
```

## Permissions

The virtual-environment Python interpreter may require permission under:

```text
System Settings
→ Privacy & Security
→ Accessibility
```

and, if requested:

```text
System Settings
→ Privacy & Security
→ Input Monitoring
```

Grant access to `.venv/bin/python` from this repository. If permissions are
granted after launch, restart the daemon:

```sh
./restart.sh
```

or:

```sh
launchctl kickstart -k gui/$(id -u)/uk.co.lailey.active-host-daemon
```

## Updating

After updating the repository, refresh dependencies and reinstall the LaunchAgent
so its rendered definition matches the current project:

```sh
git pull
.venv/bin/python -m pip install -r requirements.txt
./install.sh
```

The installer safely unloads an existing copy before loading the replacement.

## Troubleshooting

Start with the formatted status report:

```sh
./status.sh
```

The primary logs are:

```text
~/Library/Logs/active-host-daemon.log
~/Library/Logs/active-host-daemon-error.log
```

Follow them live with:

```sh
tail -f ~/Library/Logs/active-host-daemon.log
tail -f ~/Library/Logs/active-host-daemon-error.log
```

Common checks:

- If the service is not installed, run `./install.sh`.
- If permission guidance appears, grant Accessibility and Input Monitoring access,
  then run `./restart.sh`.
- If configuration validation fails, correct `config.yaml` and restart.
- If the webhook fails, verify its URL and confirm Home Assistant is reachable.
- For detailed Quartz callback and health diagnostics, temporarily set the logging
  level to `DEBUG`, then restart the daemon.

## LaunchAgent Commands

Inspect the launchd service directly:

```sh
launchctl print gui/$(id -u)/uk.co.lailey.active-host-daemon
```

Restart it directly:

```sh
launchctl kickstart -k gui/$(id -u)/uk.co.lailey.active-host-daemon
```

The installed plist is located at:

```text
~/Library/LaunchAgents/uk.co.lailey.active-host-daemon.plist
```

## Examples

Install or replace the LaunchAgent:

```sh
./install.sh
```

Start a stopped LaunchAgent:

```sh
./start.sh
```

Restart the daemon and immediately display its status:

```sh
./restart.sh
```

Stop the daemon without uninstalling it:

```sh
./stop.sh
```

Display service, process, path, permission, and recent-log information:

```sh
./status.sh
```

Unload the service and remove its installed plist while preserving logs:

```sh
./uninstall.sh
```
