#!/usr/bin/env bash

set -euo pipefail

# Resolve the repository from this script's location, regardless of the caller's
# current working directory.
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LABEL="uk.co.lailey.active-host-daemon"
SOURCE_PLIST="${PROJECT_ROOT}/launchd/${LABEL}.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
DESTINATION_PLIST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

# Fail clearly if the repository does not contain the LaunchAgent template.
if [[ ! -f "${SOURCE_PLIST}" ]]; then
    echo "Error: LaunchAgent template not found: ${SOURCE_PLIST}" >&2
    exit 1
fi

if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    echo "Error: virtual-environment Python not found." >&2
    echo "Create it with: python3 -m venv \"${PROJECT_ROOT}/.venv\"" >&2
    exit 1
fi

# launchd requires these directories to exist before installation and before it
# can open the configured stdout/stderr files.
mkdir -p "${LAUNCH_AGENTS_DIR}" "${HOME}/Library/Logs"

# Render through a temporary file so a failed substitution cannot leave a
# truncated or partially written LaunchAgent. Escape sed replacement characters
# that may legitimately occur in a project path.
TEMP_PLIST="$(mktemp "${LAUNCH_AGENTS_DIR}/.${LABEL}.XXXXXX")"
trap 'rm -f "${TEMP_PLIST}"' EXIT
ESCAPED_PROJECT_ROOT="$(printf '%s' "${PROJECT_ROOT}" | sed 's/[&|\\]/\\&/g')"
sed "s|__PROJECT_ROOT__|${ESCAPED_PROJECT_ROOT}|g" \
    "${SOURCE_PLIST}" > "${TEMP_PLIST}"
plutil -lint "${TEMP_PLIST}" > /dev/null

# Stop a previously loaded copy before replacing its on-disk definition.
if launchctl print "${DOMAIN}/${LABEL}" > /dev/null 2>&1; then
    launchctl bootout "${DOMAIN}/${LABEL}"
    echo "Unloaded existing LaunchAgent: ${LABEL}"
fi

mv "${TEMP_PLIST}" "${DESTINATION_PLIST}"
trap - EXIT
echo "Installed LaunchAgent plist: ${DESTINATION_PLIST}"

# Bootstrap loads the agent into the current GUI login session. RunAtLoad starts
# the daemon immediately, while KeepAlive restarts it if it exits.
launchctl bootstrap "${DOMAIN}" "${DESTINATION_PLIST}"
echo "Loaded LaunchAgent: ${LABEL}"
echo
echo "Installation complete."
echo
echo "The daemon has been installed as:"
echo
echo "uk.co.lailey.active-host-daemon"
echo
echo "On first launch macOS may request:"
echo
echo " • Accessibility"
echo " • Input Monitoring"
echo
echo "If permissions are granted after the daemon has already started, restart it using:"
echo
echo "launchctl kickstart -k gui/$(id -u)/uk.co.lailey.active-host-daemon"
