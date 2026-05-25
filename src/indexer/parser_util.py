# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_util.py
"""Shared AST helper for parsing EXTERNAL (third-party) source code.

Every parser in this package (`parser_python`, `parser_odoo_core`, `parser_cli`,
`parser_lint_rules`, `registry`) feeds Python source that the project does NOT
own — Odoo upstream core + third-party addons — into ``ast.parse``. That code
legitimately contains non-raw regex/SQL string literals like ``'\\s'`` or
``'\\%'`` (e.g. ``odoo/tools/sql.py`` does ``.replace('%', '\\%')`` and
``odoo/models.py`` does ``re.compile('^(\\s*...')``). On CPython 3.12+ those emit
``SyntaxWarning: invalid escape sequence`` at parse time.

Because the original ``ast.parse(source)`` calls passed NO ``filename``, those
warnings surfaced in the reindex log as bare ``<unknown>:NN: SyntaxWarning: ...``
lines interleaved with the embedding progress — pure noise from input we do not
control and cannot fix (it is someone else's source).

This helper is the single choke-point for parsing external source:

1. It passes a REAL ``filename`` into ``ast.parse`` so that any genuine diagnostic
   (e.g. a real ``SyntaxError`` we re-raise) is attributable to a concrete file
   instead of ``<unknown>``.
2. It scopes ``warnings.simplefilter("ignore", SyntaxWarning)`` to ONLY this one
   ``ast.parse`` call via ``warnings.catch_warnings()``. The filter is restored
   the instant the ``with`` block exits.

Why this does NOT violate the "never hide errors" rule (CLAUDE.md / MEMORY):

* The suppression is *scoped* to the single external-source parse — it is NOT a
  process-wide ``warnings.filterwarnings(...)`` and NOT applied to compilation of
  our OWN modules. Our own ``src/`` is verified clean (no invalid escapes); if we
  ever introduced one, it would still surface — only third-party input parsed
  through THIS helper is quietened.
* ``SyntaxError`` is NOT suppressed. The caller still sees it and keeps its
  existing behaviour (e.g. v8/v9 Python-2 text-regex fallback). We only silence
  the non-fatal ``SyntaxWarning`` noise from input we neither own nor can fix.
"""
from __future__ import annotations

import ast
import warnings


def parse_external_source(source: str, filename: str | None = None) -> ast.AST:
    """``ast.parse`` external (non-owned) source, scoping away ``SyntaxWarning``.

    Args:
        source:   Raw Python source read from an Odoo core / third-party file.
        filename: Real path for diagnostics (so re-raised errors are not
                  ``<unknown>``). Defaults to ``"<external>"`` when the caller has
                  no path handy.

    Returns:
        The parsed ``ast.Module``.

    Raises:
        SyntaxError: propagated unchanged — callers handle their own fallback.
                     (Only the non-fatal ``SyntaxWarning`` is suppressed.)
    """
    with warnings.catch_warnings():
        # Scoped to THIS parse only: silence the non-fatal escape-sequence noise
        # from third-party source. Restored automatically on block exit.
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source, filename=filename or "<external>")
