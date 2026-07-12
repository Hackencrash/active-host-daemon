#!/usr/bin/env bash

set -euo pipefail

LABEL="uk.co.lailey.active-host-daemon"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_FILE="${HOME}/Library/Logs/active-host-daemon.log"

LOADED="No"
RUNNING="No"
PID="Not running"
PROJECT_DIRECTORY="Unknown"
PYTHON_INTERPRETER="Unknown"
CONFIGURATION_FILE="Unknown"
VERSION="Unknown"

# launchctl print provides the authoritative loaded/running state and PID.
if SERVICE_STATUS="$(launchctl print "${SERVICE}" 2>/dev/null)"; then
    LOADED="Yes"
    PID_VALUE="$(awk '$1 == "pid" && $2 == "=" {print $3; exit}' <<< "${SERVICE_STATUS}")"
    if [[ -n "${PID_VALUE}" ]]; then
        PID="${PID_VALUE}"
        RUNNING="Yes"
    fi
fi

# Read installed paths from the rendered plist instead of assuming where the
# repository lives.
if [[ -f "${PLIST}" ]]; then
    PROJECT_DIRECTORY="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "${PLIST}" 2>/dev/null || echo Unknown)"
    if [[ "${PROJECT_DIRECTORY}" != "Unknown" ]]; then
        PYTHON_INTERPRETER="${PROJECT_DIRECTORY}/.venv/bin/python"
        CONFIGURATION_FILE="${PROJECT_DIRECTORY}/config.yaml"
        if [[ -x "${PYTHON_INTERPRETER}" && -f "${PROJECT_DIRECTORY}/active_host.py" ]]; then
            VERSION_OUTPUT="$("${PYTHON_INTERPRETER}" "${PROJECT_DIRECTORY}/active_host.py" --version 2>/dev/null || true)"
            VERSION="${VERSION_OUTPUT#Active Host Daemon v}"
            [[ -n "${VERSION}" ]] || VERSION="Unknown"
        fi
    fi
fi

echo "-------------------------------------------------"
echo "Active Host Daemon"
echo "-------------------------------------------------"
echo
echo "Version:             ${VERSION}"
echo "LaunchAgent loaded:  ${LOADED}"
echo "Daemon running:      ${RUNNING}"
echo "PID:                  ${PID}"
echo "Python interpreter:   ${PYTHON_INTERPRETER}"
echo "Project directory:    ${PROJECT_DIRECTORY}"
echo "Configuration file:   ${CONFIGURATION_FILE}"
echo "Log file:             ${LOG_FILE}"
echo "Accessibility:        Unknown"
echo "Input Monitoring:     Unknown"
echo
echo "Last 10 log lines:"
echo "-------------------------------------------------"
if [[ -f "${LOG_FILE}" ]]; then
    tail -n 10 "${LOG_FILE}"
else
    echo "No log file found."
fi
