#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Lint rule: fetch() with JSON body must include Content-Type: application/json
# (prevent Astro 5.x checkOrigin rejection per M8 hotfix postmortem).
#
# Exemptions (skipped — Content-Type would be wrong or unnecessary):
#   - body: FormData / new FormData(...)  — browser sets multipart/form-data
#   - No body at all (e.g. POST /logout with cookie auth only)
#
# Usage: bash scripts/lint_fetch_content_type.sh
set -euo pipefail

violations=""
for file in $(find site/src -name "*.astro" -o -name "*.ts" 2>/dev/null); do
    # Find lines with method: 'POST'/'PATCH'/'DELETE'
    method_lines=$(grep -n -E "method:\s*['\"]?(POST|PATCH|DELETE)" "$file" 2>/dev/null || true)
    if [ -z "$method_lines" ]; then continue; fi
    while IFS= read -r match; do
        lineno=${match%%:*}
        # Inspect a 6-line window around the fetch options.
        end=$((lineno + 5))
        window=$(sed -n "${lineno},${end}p" "$file")
        # Exemption 0: submitJson() wrapper. The shared helper in
        # site/src/lib/apiClient.ts sets `Content-Type: application/json` for every
        # non-binary body (and even bodyless mutations) centrally — call sites
        # intentionally omit the header. This behavior is protected by its own unit
        # tests (apiClient.test.ts cases (d) and (d3)), a stronger guarantee than this
        # grep. The opening `submitJson(` is typically 1-3 lines ABOVE the `method:`
        # line, so look backward a few lines too.
        prestart=$((lineno > 4 ? lineno - 4 : 1))
        if sed -n "${prestart},${end}p" "$file" | grep -q "submitJson"; then
            continue
        fi
        # Exemption 1: FormData body — Content-Type set automatically by browser.
        if echo "$window" | grep -qE "body:\s*(new\s+)?FormData|body:\s*\w*[Ff]ormData|body:\s*[a-zA-Z_]+Fd|body:\s*fd\b"; then
            continue
        fi
        # Exemption 2: no body in this fetch call at all (e.g. logout POST).
        if ! echo "$window" | grep -qE "body:\s*"; then
            continue
        fi
        if ! echo "$window" | grep -q "Content-Type.*application/json"; then
            violations="$violations
$file:$lineno: fetch() without Content-Type: application/json"
        fi
    done <<< "$method_lines"
done

if [ -n "$violations" ]; then
    echo "❌ lint_fetch_content_type: violations found:"
    echo "$violations"
    echo ""
    echo "Fix: add headers: { 'Content-Type': 'application/json' } to fetch options."
    exit 1
fi

echo "✓ lint_fetch_content_type: no violations"
