#!/usr/bin/env bash
#
# Restart the elec_auto launchd agent and show the new PID.
# Use after editing code or .env so the service picks up changes.

set -euo pipefail

LABEL="com.elec_auto.serve"

launchctl kickstart -k "gui/$(id -u)/${LABEL}"
sleep 2
launchctl list | grep "${LABEL}" || {
    echo "ERROR: ${LABEL} not running after restart." >&2
    exit 1
}
