#!/usr/bin/env bash
# sync.sh — Install all dependencies across layers.
#
# Replaces bare `uv sync` in the deploy ritual. Ensures Layer 2 packages
# (declared in requirements.txt files alongside TARS_OTHS modules) survive
# Core dependency reconciliation.
#
# Usage:
#   scripts/sync.sh          # from /opt/tars, or
#   TARS_OTHS=... scripts/sync.sh   # with explicit layer 2 paths

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- Resolve layer paths from systemd if not in environment ---
# Find the service unit whose WorkingDirectory matches this repo. On a
# multi-install box (e.g. tars.service + tars-tutor.service) each instance
# has its own unit pointing at a different core dir.
_resolve_service_unit() {
    for unit in $(systemctl list-units --type=service --state=loaded --no-legend 2>/dev/null \
            | awk '/tars/{print $1}'); do
        local wd env
        wd=$(systemctl show "$unit" -p WorkingDirectory --value 2>/dev/null || true)
        [ "$wd" != "$REPO_ROOT" ] && continue
        env=$(systemctl show "$unit" -p Environment --value 2>/dev/null || true)
        # Only match the main agent service — must have both layer paths
        echo "$env" | grep -qP 'TARS_OTHS=' || continue
        echo "$env" | grep -qP 'TARS_OVERLAY=' || continue
        echo "$unit"
        return
    done
}

if [ -z "${TARS_OTHS:-}" ] || [ -z "${TARS_OVERLAY:-}" ]; then
    _UNIT=$(_resolve_service_unit)
    if [ -n "${_UNIT:-}" ]; then
        _ENV=$(systemctl show "$_UNIT" -p Environment --value 2>/dev/null || true)
        if [ -z "${TARS_OTHS:-}" ]; then
            TARS_OTHS=$(echo "$_ENV" | tr ' ' '\n' | grep -oP '^TARS_OTHS=\K.*' || true)
            [ -n "$TARS_OTHS" ] && echo "[sync] TARS_OTHS resolved from $_UNIT"
        fi
        if [ -z "${TARS_OVERLAY:-}" ]; then
            TARS_OVERLAY=$(echo "$_ENV" | tr ' ' '\n' | grep -oP '^TARS_OVERLAY=\K.*' || true)
            [ -n "$TARS_OVERLAY" ] && echo "[sync] TARS_OVERLAY resolved from $_UNIT"
        fi
    fi
fi

# --- Layer 1: Core ---
echo "[sync] Layer 1: uv sync (Core)"
uv sync

# --- Layer 2: TARS_OTHS modules ---
if [ -n "${TARS_OTHS:-}" ]; then
    IFS=':' read -ra oths_dirs <<< "$TARS_OTHS"
    for dir in "${oths_dirs[@]}"; do
        # TARS_OTHS entries point to module dirs (e.g. /opt/tars-oths/my_module).
        # requirements.txt lives in the module root.
        req="$dir/requirements.txt"
        if [ -f "$req" ]; then
            echo "[sync] Layer 2: installing $req"
            uv pip install -r "$req"
        fi
    done
else
    echo "[sync] Layer 2: TARS_OTHS not set, skipping"
fi

# --- Layer 3: Overlay ---
if [ -n "${TARS_OVERLAY:-}" ] && [ -f "$TARS_OVERLAY/requirements.txt" ]; then
    echo "[sync] Layer 3: installing $TARS_OVERLAY/requirements.txt"
    uv pip install -r "$TARS_OVERLAY/requirements.txt"
fi

# --- Agent temp dirs ---
if [ -n "${TARS_OVERLAY:-}" ]; then
    mkdir -p "$TARS_OVERLAY/tmp"/{media,docs,scratch}
    echo "[sync] Agent tmp dirs ensured at $TARS_OVERLAY/tmp/"
fi

# --- Fix ownership if run as root ---
if [ "$(id -u)" -eq 0 ]; then
    echo "[sync] Ran as root — fixing ownership to tars:tars"
    chown -R tars:tars "$REPO_ROOT"
    [ -n "${TARS_OVERLAY:-}" ] && chown -R tars:tars "$TARS_OVERLAY"
fi

echo "[sync] Done"
