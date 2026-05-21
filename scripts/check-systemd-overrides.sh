#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# check-systemd-overrides.sh — Drift audit for installed OSM systemd units.
#
# Compares each installed /etc/systemd/system/odoo-semantic-*.service body
# against the shipped template in docs/deploy/, and reports:
#   - body drift     (someone edited the unit body in place — should be a drop-in)
#   - missing unit   (template exists but no installed body)
#   - orphan drop-in (drop-in exists but no installed body)
#
# Exit codes:
#   0  no drift detected
#   1  drift detected (operator must investigate)
#   2  precondition error (no docs/deploy, not on a systemd host, etc.)
#
# Usage:
#   bash scripts/check-systemd-overrides.sh
#   make check-systemd-overrides
#
# Issue #144 motivation: a routine `cp` install of upstream templates wiped
# operator customizations and caused a production outage. This script runs
# BEFORE the deploy so the operator sees the divergence and can move it to a
# drop-in override (per docs/deploy/install-runbook.md §"Drop-in overrides").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/docs/deploy"
TARGET_DIR="/etc/systemd/system"

# Units the script audits. Kept symmetric with install.sh's `*.service` glob
# (issue #145 review): every unit install.sh writes to /etc/systemd/system must
# be auditable so hand-edits cannot slip past drift detection and then be
# silently overwritten on the next install.sh run (the exact #144 risk).
# `osm-alert@.service` is intentionally excluded — it is the documented
# notifier-customization point (operators routinely replace ExecStart= to wire
# email / Slack / PagerDuty), so drift there is expected operator divergence.
# Timers (`*.timer`) have no body customization surface and are also excluded.
UNITS=(
    "odoo-semantic-mcp.service"
    "odoo-semantic-webui.service"
    "odoo-semantic-astro.service"
    "odoo-semantic-backup.service"
    "osm-ttl-cleanup.service"
)

color_red()    { printf '\033[31m%s\033[0m' "$*"; }
color_yellow() { printf '\033[33m%s\033[0m' "$*"; }
color_green()  { printf '\033[32m%s\033[0m' "$*"; }

if [[ ! -d "$DEPLOY_DIR" ]]; then
    color_red "✗ docs/deploy/ not found at $DEPLOY_DIR" >&2
    echo >&2
    exit 2
fi

if [[ ! -d "$TARGET_DIR" ]]; then
    color_yellow "⚠ $TARGET_DIR not present — not a systemd host. Skipping audit."
    echo
    exit 0
fi

drift_count=0
missing_count=0
orphan_count=0
noncanonical_count=0

echo "=== Systemd unit drift audit ==="
echo "Template dir: $DEPLOY_DIR"
echo "Installed at: $TARGET_DIR"
echo

for unit in "${UNITS[@]}"; do
    template="$DEPLOY_DIR/$unit"
    installed="$TARGET_DIR/$unit"
    dropin_dir="$TARGET_DIR/${unit}.d"

    if [[ ! -f "$template" ]]; then
        color_yellow "[?] $unit: no template at $template — skipping"
        echo
        continue
    fi

    if [[ ! -f "$installed" ]]; then
        color_yellow "[ ] $unit: not installed at $installed"
        echo
        ((missing_count++)) || true
        continue
    fi

    # Canonical-vs-substituted detection (issue #145 review): the shipped
    # template uses canonical ADR-0027 paths + `User=odoo-semantic`. A non-root
    # `install.sh --systemd` run sed-substitutes User= + every path to the dev
    # operator's layout, so a direct body diff against the canonical template
    # would ALWAYS fire false drift. We can only verify body integrity by direct
    # comparison when the installed unit is itself canonical. Detect via User=.
    installed_user=$(grep -E '^[[:space:]]*User=' "$installed" | head -1 | cut -d= -f2- | tr -d '[:space:]')
    if [[ "$installed_user" != "odoo-semantic" ]]; then
        color_yellow "[~] $unit: non-canonical install (User=${installed_user:-<unset>})"
        echo
        echo "    Body audit skipped — this unit was installed with substituted"
        echo "    paths (dev/custom layout), so a direct diff vs the canonical"
        echo "    template is not meaningful. Verify operator customizations live"
        echo "    in a drop-in at $dropin_dir/ rather than hand-edits to the body."
        echo
        ((noncanonical_count++)) || true
        if [[ -d "$dropin_dir" ]]; then
            local_dropins=$(find "$dropin_dir" -maxdepth 1 -type f -name '*.conf' 2>/dev/null | wc -l | tr -d ' ')
            [[ "$local_dropins" -gt 0 ]] && echo "    Drop-ins present: $local_dropins file(s) at $dropin_dir" && echo
        fi
        continue
    fi

    # Diff: ignore comment-only changes (lines starting with `#`) so cosmetic
    # comment churn in the upstream template doesn't fire false-positives.
    # Operator-meaningful directives are non-comment lines.
    if diff -u \
            <(grep -Ev '^[[:space:]]*#' "$installed" || true) \
            <(grep -Ev '^[[:space:]]*#' "$template" || true) \
            > /dev/null 2>&1; then
        color_green "[✓] $unit: in sync with shipped template"
        echo
    else
        color_red "[✗] $unit: BODY DRIFT detected"
        echo
        echo "    Diff (installed → shipped, ignoring comment-only changes):"
        diff -u \
            --label "$installed" \
            --label "$template" \
            <(grep -Ev '^[[:space:]]*#' "$installed" || true) \
            <(grep -Ev '^[[:space:]]*#' "$template" || true) \
            | sed 's/^/      /' || true
        echo
        echo "    → Move operator customizations to a drop-in override at"
        echo "      $dropin_dir/<your-name>.conf  (see docs/deploy/overrides/)"
        echo
        ((drift_count++)) || true
    fi

    if [[ -d "$dropin_dir" ]]; then
        local_dropins=$(find "$dropin_dir" -maxdepth 1 -type f -name '*.conf' 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$local_dropins" -gt 0 ]]; then
            echo "    Drop-ins present: $local_dropins file(s) at $dropin_dir"
        fi
        echo
    fi
done

# Detect orphan drop-in directories (drop-in dir exists but no installed unit body).
for dropin_dir in "$TARGET_DIR"/odoo-semantic-*.service.d; do
    [[ -d "$dropin_dir" ]] || continue
    unit_name=$(basename "${dropin_dir%.d}")
    if [[ ! -f "$TARGET_DIR/$unit_name" ]]; then
        color_yellow "[!] orphan drop-in: $dropin_dir exists but $TARGET_DIR/$unit_name is missing"
        echo
        ((orphan_count++)) || true
    fi
done

echo
echo "=== Summary ==="
echo "  Body drift:      $drift_count"
echo "  Not installed:   $missing_count"
echo "  Non-canonical:   $noncanonical_count (body audit skipped — verify drop-ins)"
echo "  Orphan dirs:     $orphan_count"
echo

if [[ $drift_count -gt 0 ]]; then
    color_red "✗ Drift detected — see docs/deploy/install-runbook.md §6 (Recovery)" >&2
    echo >&2
    exit 1
fi

color_green "✓ All audited units in sync (or not yet installed)"
echo
exit 0
