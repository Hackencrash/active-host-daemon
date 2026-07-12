#!/usr/bin/env bash

set -euo pipefail

LABEL="uk.co.lailey.active-host-daemon"
DOMAIN="gui/$(id -u)"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

echo "Stopping Active Host Daemon..."
if launchctl print "${DOMAIN}/${LABEL}" > /dev/null 2>&1; then
    launchctl bootout "${DOMAIN}" "${PLIST}"
    echo "Active Host Daemon stopped."
else
    echo "Active Host Daemon is not loaded."
fi
