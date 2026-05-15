#!/usr/bin/env bash
# Lint rule: every JSONResponse call passing a dict must wrap with _json_safe
# (prevent datetime serialization TypeError per M8 hotfix postmortem).
#
# Usage: bash scripts/lint_json_response.sh
set -euo pipefail

# Find JSONResponse({...}) — exclude lines that have _json_safe in the call
# or have noqa comment.
violations=$(grep -rn 'JSONResponse(' src/ tests/ 2>/dev/null \
    | grep -v '_json_safe' \
    | grep -v '# noqa' \
    | grep -E 'JSONResponse\([^"]*\{' || true)

if [ -n "$violations" ]; then
    echo "❌ lint_json_response: found JSONResponse(dict) without _json_safe wrap:"
    echo "$violations"
    echo ""
    echo "Fix: wrap dict with _json_safe() from src/web_ui/_json.py"
    echo "Or add '# noqa: lint-json-response' if datetime not possible."
    exit 1
fi

echo "✓ lint_json_response: no violations"
