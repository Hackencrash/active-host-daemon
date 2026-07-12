#!/usr/bin/env bash

set -euo pipefail

LABEL="uk.co.lailey.active-host-daemon"
DESTINATION_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

# Unload the service from the current GUI login session when it is registered.
if launchctl print "${DOMAIN}/${LABEL}" > /dev/null 2>&1; then
    launchctl bootout "${DOMAIN}/${LABEL}"
    echo "Unloaded LaunchAgent: ${LABEL}"
else
    echo "LaunchAgent is not currently loaded: ${LABEL}"
fi

# Remove only the installed plist. Logs are intentionally preserved.
if [[ -f "${DESTINATION_PLIST}" ]]; then
    rm "${DESTINATION_PLIST}"
    echo "Removed LaunchAgent plist: ${DESTINATION_PLIST}"
fi

echo "active-host-daemon uninstalled successfully; logs were preserved."
