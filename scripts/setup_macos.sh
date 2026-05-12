#!/usr/bin/env bash
#
# One-shot setup for the always-on dashboard host (macOS).
# Run from the repo root:  bash scripts/setup_macos.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="${REPO_DIR}/scripts/com.elec_auto.serve.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.elec_auto.serve.plist"
LOG_DIR="${HOME}/Library/Logs"

echo "==> elec_auto always-on setup"
echo "    repo: ${REPO_DIR}"
echo

# ---- 1. Sanity checks ------------------------------------------------------
if [[ ! -f "${REPO_DIR}/.env" ]]; then
    echo "ERROR: ${REPO_DIR}/.env not found. Copy/fill it before running this." >&2
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: 'uv' not found on PATH. Install with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
UV_PATH="$(command -v uv)"
if [[ "${UV_PATH}" != "/usr/local/bin/uv" ]]; then
    echo "NOTE: uv is at ${UV_PATH}; plist hard-codes /usr/local/bin/uv."
    echo "      Either symlink it or edit the plist's ProgramArguments."
fi

# ---- 2. pmset: never sleep, even with lid closed --------------------------
echo "==> Configuring pmset (requires sudo)"
# disablesleep=1 keeps the system awake unconditionally — including when
# the lid is closed. Required for a headless always-on host. Only safe
# when the machine is on AC; on battery it'll run until the pack dies.
sudo pmset -a disablesleep 1
sudo pmset -c sleep 0          # system never sleeps on AC
sudo pmset -c disksleep 0      # disks always spinning
sudo pmset -c hibernatemode 0  # no hibernation
sudo pmset -c powernap 0       # disable power nap (we want the real CPU)
sudo pmset -c womp 1           # wake on magic packet (LAN wake)
sudo pmset -c displaysleep 10  # display can still sleep after 10 min

echo
echo "Current power settings:"
pmset -g | grep -E '^ (sleep|disksleep|displaysleep|hibernatemode|powernap|womp|disablesleep)'
echo

# ---- 3. Install launchd agent ---------------------------------------------
mkdir -p "${LOG_DIR}"
mkdir -p "$(dirname "${PLIST_DST}")"

if launchctl list | grep -q com.elec_auto.serve; then
    echo "==> Existing launchd agent found; unloading first"
    launchctl unload "${PLIST_DST}" 2>/dev/null || true
fi

cp "${PLIST_SRC}" "${PLIST_DST}"
launchctl load "${PLIST_DST}"

echo "==> launchd agent installed: ${PLIST_DST}"
echo

# ---- 4. Show status -------------------------------------------------------
sleep 2
echo "==> launchctl status:"
launchctl list | grep com.elec_auto || echo "    (not yet listed — give it a moment)"

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<unknown>')"
echo
echo "Dashboard should be reachable at:"
echo "    http://127.0.0.1:8000/"
echo "    http://${LAN_IP}:8000/"
echo
echo "Tail logs with:"
echo "    tail -f ${LOG_DIR}/elec_auto.log"
echo
echo "To stop:  launchctl unload ${PLIST_DST}"
echo "To start: launchctl load   ${PLIST_DST}"
