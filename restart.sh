#!/usr/bin/env bash

set -euo pipefail

# Resolve status.sh relative to this script so restart works from any directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SERVICE="gui/$(id -u)/uk.co.lailey.active-host-daemon"

echo "Restarting Active Host Daemon..."
launchctl kickstart -k "${SERVICE}"
echo
"${SCRIPT_DIR}/status.sh"
