#!/usr/bin/env bash

set -euo pipefail

LABEL="uk.co.lailey.active-host-daemon"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

echo "Starting Active Host Daemon..."
if launchctl print "${SERVICE}" > /dev/null 2>&1; then
    launchctl kickstart "${SERVICE}"
else
    if [[ ! -f "${PLIST}" ]]; then
        echo "Error: LaunchAgent is not installed. Run ./install.sh first." >&2
        exit 1
    fi
    launchctl bootstrap "${DOMAIN}" "${PLIST}"
fi
echo "Active Host Daemon started."
