#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Lint rule: every JSONResponse call passing a dict literal must wrap with
# _json_safe() (prevent datetime/Decimal/UUID/bytes serialization TypeError
# per M8 hotfix postmortem). Catches BOTH single-line and multi-line forms:
#
#   JSONResponse({...})                       — single-line
#   JSONResponse(\n    {...})                 — multi-line, dict literal opens on next line
#   JSONResponse(\n    {...},\n status=400)   — multi-line with trailing kwargs
#
# Per-line `# noqa` (anywhere on the JSONResponse line OR on the `{` line)
# suppresses the violation — used for test-stub handlers that intentionally
# return plain literals.
#
# Usage: bash scripts/lint_json_response.sh
set -euo pipefail

violations=$(python3 - <<'PY'
import re, sys
from pathlib import Path

files = list(Path("src").rglob("*.py")) + list(Path("tests").rglob("*.py"))
findings = []
for f in files:
    text = f.read_text()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        # Skip already-wrapped or explicitly suppressed lines.
        if "_json_safe" in line or "# noqa" in line:
            continue
        m = re.search(r"JSONResponse\s*\(", line)
        if not m:
            continue
        # Find first non-whitespace char after the `(` — either same line
        # (single-line form) or on a subsequent line (multi-line form).
        # We skip blank/comment lines between `(` and the first token.
        after = line[m.end():].lstrip()
        if not after or after.startswith("#"):
            # Look ahead up to 5 lines.
            j = i + 1
            while j < min(i + 6, len(lines)):
                nxt = lines[j]
                # Comment or blank: keep searching.
                stripped = nxt.lstrip()
                if not stripped or stripped.startswith("#"):
                    j += 1
                    continue
                # Per-line suppression on the dict opener.
                if "# noqa" in nxt or "_json_safe" in nxt:
                    after = None
                    break
                after = stripped
                break
            if after is None:
                continue
        if after and after.startswith("{"):
            findings.append(f"{f}:{i + 1}:{line.rstrip()}")

if findings:
    print("\n".join(findings))
    sys.exit(1)
PY
) && rc=0 || rc=$?

if [ "${rc:-0}" -ne 0 ]; then
    echo "❌ lint_json_response: found JSONResponse(dict) without _json_safe wrap:"
    echo "$violations"
    echo ""
    echo "Fix: wrap dict with _json_safe() from src/web_ui/_json.py"
    echo "Or add '# noqa' on the JSONResponse line for test stubs (lint-json-response bypass)."
    exit 1
fi

echo "✓ lint_json_response: no violations"
