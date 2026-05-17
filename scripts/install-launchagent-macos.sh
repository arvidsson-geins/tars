#!/usr/bin/env bash
#
# Install T.A.R.S as a macOS LaunchAgent so it auto-starts at login and
# auto-restarts on crash. Pairs with macOS Keychain auto-unlock so the
# service comes back without operator input after reboots or process death.
#
# Prerequisite — store the vault passphrase in the login keychain first:
#     security add-generic-password -s tars-vault -a default -U
#
# Usage:
#     scripts/install-launchagent-macos.sh           # install + load
#     scripts/install-launchagent-macos.sh uninstall # unload + remove
#
set -euo pipefail

LABEL="com.tars.agent"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARS_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$TARS_HOME/config/com.tars.agent.plist.example"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

uninstall() {
    if [[ -f "$DEST" ]]; then
        launchctl unload "$DEST" 2>/dev/null || true
        rm -f "$DEST"
        echo "Removed $DEST"
    else
        echo "Nothing to uninstall ($DEST not present)"
    fi
}

if [[ "${1:-}" == "uninstall" ]]; then
    uninstall
    exit 0
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "Template missing: $TEMPLATE" >&2
    exit 1
fi

UV_PATH="$(command -v uv || true)"
if [[ -z "$UV_PATH" ]]; then
    echo "uv not found in PATH — install uv first" >&2
    exit 1
fi

if ! security find-generic-password -s tars-vault -a default >/dev/null 2>&1; then
    cat <<EOF >&2
WARNING: no vault passphrase in keychain (service=tars-vault, account=default).
TARS will fall back to an interactive prompt and the LaunchAgent will not
be able to unlock the vault unattended. Store it first:

    security add-generic-password -s tars-vault -a default -U

Continuing install anyway — you can run this script again after storing.
EOF
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Substitute paths into the template. Use a delimiter that won't clash with
# absolute paths.
sed \
    -e "s|__UV_PATH__|$UV_PATH|g" \
    -e "s|__TARS_HOME__|$TARS_HOME|g" \
    -e "s|__PATH__|$PATH|g" \
    "$TEMPLATE" > "$DEST"

# Reload (unload-then-load is idempotent)
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Installed: $DEST"
echo "Status:    launchctl list | grep $LABEL"
echo "Logs:      tail -F $TARS_HOME/data/launchd.{out,err}.log"
echo "Uninstall: $0 uninstall"
