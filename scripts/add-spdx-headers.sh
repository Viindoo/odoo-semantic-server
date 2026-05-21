#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# scripts/add-spdx-headers.sh
# Idempotently prepend SPDX headers to Python + site/* source files.
# Safe to re-run: skips files that already contain SPDX-License-Identifier.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ADDED=0
SKIPPED=0

py_header='# SPDX-License-Identifier: AGPL-3.0-or-later'
ts_header='// SPDX-License-Identifier: AGPL-3.0-or-later'

prepend_py() {
  local f="$1"
  if grep -q "SPDX-License-Identifier" "$f"; then
    SKIPPED=$((SKIPPED+1))
    return
  fi
  local first
  first=$(head -1 "$f")
  if printf '%s' "$first" | grep -q '^#!'; then
    # Shebang on line 1 — insert SPDX as line 2 (after shebang, preserves executability)
    awk 'NR==1{print; print "# SPDX-License-Identifier: AGPL-3.0-or-later"; next} {print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    printf '%s\n' "$py_header" | cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
  ADDED=$((ADDED+1))
}

prepend_ts() {
  local f="$1"
  if grep -q "SPDX-License-Identifier" "$f"; then
    SKIPPED=$((SKIPPED+1))
    return
  fi
  printf '%s\n' "$ts_header" | cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  ADDED=$((ADDED+1))
}

prepend_astro() {
  local f="$1"
  if grep -q "SPDX-License-Identifier" "$f"; then
    SKIPPED=$((SKIPPED+1))
    return
  fi
  local first
  first=$(head -1 "$f")
  if [ "$first" = "---" ]; then
    # Insert SPDX as line 2 (inside frontmatter, after opening ---)
    # Use awk: print first line (---), then the SPDX comment, then rest
    awk 'NR==1{print; print "// SPDX-License-Identifier: AGPL-3.0-or-later"; next} {print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    # No frontmatter — prepend HTML comment
    printf '%s\n' "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->" | cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
  ADDED=$((ADDED+1))
}

prepend_sh() {
  local f="$1"
  if grep -q "SPDX-License-Identifier" "$f"; then
    SKIPPED=$((SKIPPED+1))
    return
  fi
  local first
  first=$(head -1 "$f")
  if printf '%s' "$first" | grep -q '^#!'; then
    # Shebang on line 1 — insert SPDX as line 2 (after shebang)
    awk 'NR==1{print; print "# SPDX-License-Identifier: AGPL-3.0-or-later"; next} {print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    printf '%s\n' "$py_header" | cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
  ADDED=$((ADDED+1))
}

# Python files — src/
while IFS= read -r -d '' f; do
  prepend_py "$f"
done < <(find "$REPO/src" -name "*.py" -print0)

# Python files — tests/
while IFS= read -r -d '' f; do
  prepend_py "$f"
done < <(find "$REPO/tests" -name "*.py" -print0)

# Python files — scripts/
while IFS= read -r -d '' f; do
  prepend_py "$f"
done < <(find "$REPO/scripts" -name "*.py" -print0)

# Shell scripts — scripts/
while IFS= read -r -d '' f; do
  prepend_sh "$f"
done < <(find "$REPO/scripts" -name "*.sh" -print0)

# Site TS/TSX
while IFS= read -r -d '' f; do
  prepend_ts "$f"
done < <(find "$REPO/site/src" \( -name "*.ts" -o -name "*.tsx" \) -print0)

# Site Astro
while IFS= read -r -d '' f; do
  prepend_astro "$f"
done < <(find "$REPO/site/src" -name "*.astro" -print0)

echo "Done. Added: $ADDED  Skipped (already had SPDX): $SKIPPED"
