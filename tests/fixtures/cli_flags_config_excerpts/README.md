# cli_flags content-parity CI fixtures (issue #364 S3)

## What these are, and why they are NOT full-file copies

`tests/test_cli_flags_content_parity.py` diffs curated `spec_data/cli_flags_<version>.json`
"global" (`command_name` in `{null, "server"}`) flags against the live oracle
(`src/indexer/parser_cli.py::_parse_options_calls`), field by field
(`status`/`default`/`type`/`help`). The dev-box layer runs that diff against a real
`<pkg>/tools/config.py` checkout. The CI layer needs something to diff against when no
checkout is present - that is what these 12 files are.

**They are NOT excerpts in the `tests/fixtures/odoo_tests_headers/` sense** (class
signatures with bodies reduced to `pass` - see that directory's README). For a CLI flag,
the `add_option()`/`add_argument()` call **is** the fact under test: its `default=`/
`my_default=`/`type=`/`help=` keyword arguments are exactly what is being verified, so
nothing inside a chosen call can be reduced without destroying the very thing the test
checks.

## Fixture-cost measurement that led to this design (see also the test file's module
docstring)

- A full-file copy of every version's real `tools/config.py`: **482,360 bytes** across the
  12 checkouts (`~/git/odoo{8..19}`), 31-56 KB per version.
- Even a "calls only" extraction (every `add_option`/`add_argument` argument-list span,
  via the production `_find_option_call_arg_spans` helper, with no other file content):
  **179,843 bytes** (37% of the full-file total) - still 11x the entire
  `odoo_tests_headers/` precedent (76 KB for **15** files covering a *different* curated
  family, framework_bases, where irrelevant method bodies genuinely could be reduced to
  `pass`).
- **Decision: do not commit either.** A faithful per-version fixture at the scale
  `cli_flags` operates at (66-165 curated entries/version, all of them content-bearing) is
  not the same class of problem `odoo_tests_headers/` solved (menus of ~10 stable class
  names/version). Forcing that pattern here would mean re-committing most of Odoo's own
  `config.py` per version and re-syncing it on every upstream change - a maintenance
  burden with no reduction available to shrink it.

## What is committed instead

A **small, real, verbatim subset**: for each of the 12 surveyed majors, up to 5 flags
confirmed (via the same diff the test performs) to have >=1 curated-vs-oracle field
mismatch today, plus exactly 1 flag confirmed to have **zero** mismatches (a negative
control, proving the comparison does not just flag everything). Their
`add_option()`/`add_argument()` **argument-list text only** (not the whole statement, not
surrounding file content) is copied byte-for-byte from real source via the production
`_find_option_call_arg_spans()` span-finder (never hand-transcribed - eliminates
transcription risk) and wrapped in a bare `group.add_option(<verbatim args>)` statement.
These files are only ever `ast.parse()`'d by the parser under test, never imported or
executed, so `group` does not need to resolve to a real object and a `self.<method>`
reference inside a real `callback=` kwarg (e.g. `--addons-path`) is harmless.

Total size: **18,797 bytes** across all 12 files (~1.4-1.9 KB/version) - in the same size
class as the `odoo_tests_headers/` precedent, not the 180-482 KB full/calls-only
alternative above.

This means the CI layer proves something real and small (these specific flags really do
disagree today, and the healthy control really does not), not something total (it does not
claim per-version completeness - that is the dev-box layer's job, against the full,
un-excerpted, real checkout).

## Selection rule (deterministic, reproducible)

For each major: run the exact same diff `test_cli_flags_content_parity.py` uses
(`_load_curated_global_flags` vs `_oracle_global_flags`) over the FULL real
`tools/config.py`, take the first 5 flag names (file order) with >=1 mismatching field,
plus the first flag name (file order) with zero mismatching fields. Extract each chosen
flag's argument-list span via `_find_option_call_arg_spans` and write it into
`v<major>_config_excerpt.py`. This is a data-derived choice (not curator judgment) - see
`_build_ci_fixtures` methodology recorded below; regenerating from a refreshed checkout
would very likely choose different flags without invalidating the test's intent (the test
asserts *specific, named* flags by design - see the test file's `_CHOSEN_*` tables - so a
future regeneration must update those tables in the same commit, never silently).

## Chosen flags per version (mirrors `_CHOSEN_MISMATCH_FLAGS` / `_CHOSEN_HEALTHY_FLAG` in
the test file - kept here for human review, not re-read by the test)

| Major | Confirmed-mismatching (chosen) | Healthy control |
|---|---|---|
| 8  | `--addons-path`, `--auto-reload`, `--data-dir`, `--database`, `--db_host` | `--cert-file` |
| 9  | `--addons-path`, `--data-dir`, `--database`, `--db_host`, `--db_password` | `--config` |
| 10 | `--addons-path`, `--data-dir`, `--database`, `--db_host`, `--db_password` | `--config` |
| 11 | `--addons-path`, `--data-dir`, `--database`, `--db-filter`, `--db_host` | `--config` |
| 12 | `--addons-path`, `--data-dir`, `--database`, `--db-filter`, `--db_host` | `--config` |
| 13 | `--addons-path`, `--data-dir`, `--database`, `--db-filter`, `--db_host` | `--config` |
| 14 | `--addons-path`, `--data-dir`, `--database`, `--db-filter`, `--db_host` | `--config` |
| 15 | `--addons-path`, `--data-dir`, `--database`, `--db-filter`, `--db_host` | `--config` |
| 16 | `--data-dir`, `--database`, `--db-filter`, `--db_host`, `--db_password` | `--addons-path` |
| 17 | `--data-dir`, `--database`, `--db-filter`, `--db_host`, `--db_maxconn_gevent` | `--addons-path` |
| 18 | `--data-dir`, `--database`, `--db-filter`, `--db_host`, `--db_maxconn_gevent` | `--addons-path` |
| 19 | `--addons-path`, `--data-dir`, `--database`, `--db-filter`, `--db_app_name` | `--config` |

## Source provenance (real checkout -> commit SHA, `git log -1 --format=%H`)

Same checkouts (`~/git/odoo{8..19}`), same HEAD SHAs, as
`tests/fixtures/odoo_tests_headers/README.md` (both extracted in the same session):

| fixture | real source file | repo HEAD commit SHA |
|---|---|---|
| `v8_config_excerpt.py`  | `openerp/tools/config.py` (odoo8)  | `e5de6ca8013ab7e2d64937d14c4a54cceeabca5d` |
| `v9_config_excerpt.py`  | `openerp/tools/config.py` (odoo9)  | `8066c48261877a93431df12a7fdc54e86363f497` |
| `v10_config_excerpt.py` | `odoo/tools/config.py` (odoo10)    | `6a8e31a8f7b6680f6316bc1b021d9986043c2ab0` |
| `v11_config_excerpt.py` | `odoo/tools/config.py` (odoo11)    | `5a3b4ffecb06b6af1a45c9b8c6a2369b10892eaf` |
| `v12_config_excerpt.py` | `odoo/tools/config.py` (odoo12)    | `d9d73e8973036306e96bbbfdee39036b59b1f749` |
| `v13_config_excerpt.py` | `odoo/tools/config.py` (odoo13)    | `ce9f9aed0eba5f1561112666e52696ce966ef99e` |
| `v14_config_excerpt.py` | `odoo/tools/config.py` (odoo14)    | `e40997ff0366fad87b080e94291ffd6bd68b7d9e` |
| `v15_config_excerpt.py` | `odoo/tools/config.py` (odoo15)    | `6f6d921bd7f40c472ed7f0c829ab8e0ec125385e` |
| `v16_config_excerpt.py` | `odoo/tools/config.py` (odoo16)    | `dd845e5991f9195caf417e44a088b8e0196fd8bb` |
| `v17_config_excerpt.py` | `odoo/tools/config.py` (odoo17)    | `d5989146752ae0c91d5721f8d08821b95518b2bc` |
| `v18_config_excerpt.py` | `odoo/tools/config.py` (odoo18)    | `fca652c0bef69066ced92cc162a18da0a4101275` |
| `v19_config_excerpt.py` | `odoo/tools/config.py` (odoo19)    | `14ac0f3eb5ec6ef12023d0fec59fbe1e7504ff39` |

## What happens if the underlying curated JSON is later corrected

The follow-up data-correction commit (out of scope for this work item - see the test
file's module docstring) will very likely turn some of the "confirmed-mismatching" flags
above into matches. When that happens the CI-layer test that asserts these specific flags
still mismatch will go RED **for the right reason** (the fixture now disagrees with the
test's own expectation, not with reality) - at that point regenerate the fixture + this
table + the test's `_CHOSEN_*` tables together, in the same commit as the data fix, so the
CI layer keeps a live, real, always-on mismatch to prove against rather than quietly
losing coverage.
