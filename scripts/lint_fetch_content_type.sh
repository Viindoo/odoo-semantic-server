#!/usr/bin/env bash
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
