# Odoo test-framework-base fixtures

## What these are

Minimal, AST-faithful excerpts of Odoo's `odoo/tests/common.py` (`openerp/tests/common.py`
pre-v10) and `odoo/tests/form.py` (v17+), one file per major version. They exist so the T7
parity test for `src/indexer/framework_bases.py` (issue #362) can run **in CI**, where no real
Odoo checkout is available. Without a committed fixture, the parity assertion has nothing to
diff against and silently skips - defeating the point of a drift alarm on the curated menu
table in `docs/adr/` / `api-contract.md`.

`framework_bases.parse_framework_bases(odoo_source_root, odoo_version)` is a pure AST reader -
it never imports or executes the file. These fixtures only need to be syntactically valid
Python that `ast.parse` accepts; the classes and their (unresolved) base-class names do not
need to exist as importable symbols.

## What "AST-faithful excerpt" means

For every class in `{BaseCase, TransactionCase, SingleTransactionCase, SavepointCase, HttpCase,
HttpCaseCommon, HttpSavepointCase, TreeCase, Form, O2MForm}` that exists in the real file at
that version:

- The `class` statement - name, base classes, and keyword arguments (e.g. `metaclass=MetaCase`)
  - is copied **verbatim** from the real source (including deliberately-inconsistent framing
  across versions, e.g. `unittest.TestCase` vs `unittest2.TestCase` vs `case.TestCase`, and the
  v11-v14 base-class chain through `TreeCase`).
- `setUpClass` is reduced to `@classmethod def setUpClass(cls): pass` **iff** the real class
  defines its own `setUpClass` (checked against source, not assumed from the contract table).
- The `__init_subclass__` + `warnings.warn(..., DeprecationWarning, ...)` block is copied
  **verbatim**, byte-for-byte, where the real class has one. Only `SavepointCase` and
  `HttpSavepointCase` at v15/v16 carry this block.
- Everything else - docstrings (kept to their real first line where one exists), other
  attributes, non-framework methods - is reduced to `pass` or a one-line docstring. Method
  BODIES do not matter to the parser and are never asserted on.
- Relative class order within a file matches the real file (base classes appear before their
  subclasses), but **line numbers do not match** the real file and are not meant to - the parity
  test compares `{name, status, file_path, has_setUpClass}`, never `line`.

Classes are extracted straight from each real checkout via `ast.parse` + `ast.unparse` (not
hand-transcribed), so the base-class text and `has_setUpClass`/deprecation-block detection are
mechanically verified against source, not guessed.

## Source provenance (real file -> commit SHA, `git log -1 --format=%H`)

| fixture | real source file | repo HEAD commit SHA |
|---|---|---|
| `v8_common.py` | `openerp/tests/common.py` (odoo8) | `e5de6ca8013ab7e2d64937d14c4a54cceeabca5d` |
| `v9_common.py` | `openerp/tests/common.py` (odoo9) | `8066c48261877a93431df12a7fdc54e86363f497` |
| `v10_common.py` | `odoo/tests/common.py` (odoo10) | `6a8e31a8f7b6680f6316bc1b021d9986043c2ab0` |
| `v11_common.py` | `odoo/tests/common.py` (odoo11) | `5a3b4ffecb06b6af1a45c9b8c6a2369b10892eaf` |
| `v12_common.py` | `odoo/tests/common.py` (odoo12) | `d9d73e8973036306e96bbbfdee39036b59b1f749` |
| `v13_common.py` | `odoo/tests/common.py` (odoo13) | `ce9f9aed0eba5f1561112666e52696ce966ef99e` |
| `v14_common.py` | `odoo/tests/common.py` (odoo14) | `e40997ff0366fad87b080e94291ffd6bd68b7d9e` |
| `v15_common.py` | `odoo/tests/common.py` (odoo15) | `6f6d921bd7f40c472ed7f0c829ab8e0ec125385e` |
| `v16_common.py` | `odoo/tests/common.py` (odoo16) | `dd845e5991f9195caf417e44a088b8e0196fd8bb` |
| `v17_common.py` | `odoo/tests/common.py` (odoo17) | `d5989146752ae0c91d5721f8d08821b95518b2bc` |
| `v18_common.py` | `odoo/tests/common.py` (odoo18) | `fca652c0bef69066ced92cc162a18da0a4101275` |
| `v19_common.py` | `odoo/tests/common.py` (odoo19) | `14ac0f3eb5ec6ef12023d0fec59fbe1e7504ff39` |
| `v17_form.py` | `odoo/tests/form.py` (odoo17) | `d5989146752ae0c91d5721f8d08821b95518b2bc` |
| `v18_form.py` | `odoo/tests/form.py` (odoo18) | `fca652c0bef69066ced92cc162a18da0a4101275` |
| `v19_form.py` | `odoo/tests/form.py` (odoo19) | `14ac0f3eb5ec6ef12023d0fec59fbe1e7504ff39` |

Each SHA is the `git log -1 --format=%H` HEAD of the corresponding local checkout
(`~/git/odoo{8..19}`) used to extract these excerpts - not a per-file blame SHA.

## Python-2-era note (v8/v9)

`openerp/tests/common.py` on v8/v9 is Python-2-flavored source (the real files use
`unittest2.TestCase` on v8 and `unittest.TestCase` on v9, and are written for py2 execution),
but the relevant `class` statements are ordinary py2/py3-compatible syntax - `ast.parse` under
Python 3 parses both real files and both fixtures without modification. No simplification was
needed.

## Verification performed

- All 15 fixtures parse under `ast.parse` (Python 3) with zero syntax errors.
- For every fixture, the AST class-name set was diffed against this feature's authoritative
  per-era menu (`api-contract.md` § "Authoritative menus per era") - **zero mismatches** found
  at any era (E1 through E7).
- For every fixture, `has_setUpClass` and the `__init_subclass__`/`DeprecationWarning` block
  presence were independently re-derived from the real source per version and diffed against
  the contract's curated `has_setUpClass` table - **zero mismatches** found.
- For every class in every fixture, base classes and keyword arguments were diffed verbatim
  (via `ast.unparse`) against the real source - **exact match**, all 15 files.

No contract disagreement was found; the curated menus and `has_setUpClass` table in
`api-contract.md` are confirmed accurate against real source for every indexed version (v8-v19).
