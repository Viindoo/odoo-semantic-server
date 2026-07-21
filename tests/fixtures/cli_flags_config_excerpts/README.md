# cli_flags content-parity CI fixtures (issue #364 S3)

## Status (post data-correction)

The curated `spec_data/cli_flags_<version>.json` files were corrected (967 field
corrections across all 12 versions, regenerated from the production oracle). The CI-layer
test (`test_ci_excerpt_matches_curated_global_cli_flags_per_version`) was inverted to
match: it now asserts PARITY (curated == oracle for every fixture-covered flag, field by
field) instead of "reproduces the known mismatch" - a test that can only be green while a
bug exists protects the bug, not the business rule. All 6 fixture-covered flags per
version, on all 12 versions, are confirmed (by the same diff the test performs) to agree
with the corrected curated data today. The fixture `.py` excerpt files below were
**not** regenerated - they are verbatim, `_find_option_call_arg_spans`-extracted real
source and were unaffected by the JSON correction; re-running the extraction against the
same checkout HEAD SHAs (see "Source provenance" below) reproduces them byte-for-byte.
See "Negative control" below for the reproducible proof that this test can still fail.

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

A **small, real, verbatim subset**: for each of the 12 surveyed majors, 6 flags - the same
6 chosen at issue #364 S3 fixture-build time (5 originally confirmed, via the same diff the
test performs, to have >=1 curated-vs-oracle field mismatch, plus 1 originally confirmed to
have **zero** mismatches - see "Status" above and "Chosen flags per version" below for
current parity status). Their `add_option()`/`add_argument()` **argument-list text only**
(not the whole statement, not surrounding file content) is copied byte-for-byte from real
source via the production `_find_option_call_arg_spans()` span-finder (never
hand-transcribed - eliminates transcription risk) and wrapped in a bare
`group.add_option(<verbatim args>)` statement. These files are only ever `ast.parse()`'d by
the parser under test, never imported or executed, so `group` does not need to resolve to a
real object and a `self.<method>` reference inside a real `callback=` kwarg (e.g.
`--addons-path`) is harmless.

Total size: **18,797 bytes** across all 12 files (~1.4-1.9 KB/version) - in the same size
class as the `odoo_tests_headers/` precedent, not the 180-482 KB full/calls-only
alternative above.

This means the CI layer proves something real and small (these specific flags' curated data
really does agree with real source today, on every CI run, with zero dependency on a
checkout), not something total (it does not claim per-version completeness - that is the
dev-box layer's job, against the full, un-excerpted, real checkout).

## Selection rule (deterministic, reproducible)

Historical (issue #364 S3, RED phase): for each major, run the exact same diff
`test_cli_flags_content_parity.py` uses (`_load_curated_global_flags` vs
`_oracle_global_flags`) over the FULL real `tools/config.py`, take the first 5 flag names
(file order) with >=1 mismatching field, plus the first flag name (file order) with zero
mismatching fields. Extract each chosen flag's argument-list span via
`_find_option_call_arg_spans` and write it into `v<major>_config_excerpt.py`. This was a
data-derived choice (not curator judgment); the test asserts *specific, named* flags by
design - see the test file's `_CHOSEN_FIXTURE_FLAGS` table - so any future regeneration
(different flags, e.g. after a checkout refreshes to a newer point release) must update
that table + this README's table + the `.py` excerpts together, in the same commit, never
silently. Since the S3 data-correction commit, this rule no longer has a "first N
mismatching" set to select from (0 mismatches exist against real source today) - a future
regeneration should instead pick any deterministic, diverse sample (e.g. first N flags in
file order) via the identical extraction method; the committed fixtures were left as-is
(see "Status" above) since they remain faithful, verbatim, real-source excerpts that still
exercise the full diff machinery meaningfully.

## Chosen flags per version (mirrors `_CHOSEN_FIXTURE_FLAGS` in the test file - kept here
for human review, not re-read by the test)

All 6 flags per version now agree with curated data on every compared field
(status/default/type/help) - the "originally mismatching" / "healthy control" columns
below record provenance (why each flag was picked), not current behavior.

| Major | Originally confirmed-mismatching (now: match) | Originally healthy control (still: match) |
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

## Negative control (proof this test can still fail)

A parity test that can never go RED is theater, not a guard. Reproduced when this file was
last inverted (issue #364 S3 follow-up), without ever touching the committed
`src/indexer/spec_data/*.json`:

1. Copy `src/indexer/spec_data/cli_flags_19.0.json` to a scratch directory (outside the
   repo working tree).
2. In the scratch copy only, corrupt one compared field of one fixture-covered flag - e.g.
   change `--addons-path`'s `help` text to something the real v19 oracle does not say.
3. Monkeypatch `tests.test_cli_flags_content_parity._load_curated_global_flags` to read
   from the scratch directory instead of `SPEC_DATA_DIR` (the real loader's `spec_dir`
   default argument is bound at function-definition time, so reassigning the module-level
   `SPEC_DATA_DIR` constant alone does *not* propagate - the loader function itself must be
   swapped), then call the real, unmodified
   `test_ci_excerpt_matches_curated_global_cli_flags_per_version(19)` directly.
4. Observe: `AssertionError` - `v19: curated cli_flags_19.0.json must agree field-by-field
   with the committed fixture excerpt ... got mismatches ... '--addons-path' help
   curated='<corrupted text>' oracle='specify additional addons paths (separated by
   commas).'`
5. Restore the monkeypatch; re-run the same test unpatched against the real committed
   `spec_data/` - GREEN, no `AssertionError`.

This proves the diff machinery genuinely discriminates (perturbed data -> RED, real data ->
GREEN), not that the assertion is vacuously true. Repeat this recipe (never edit the real
`spec_data/*.json` in place) whenever this test's ability to fail needs re-demonstrating -
e.g. after inverting a similar test in another curated family.

## What happens if the underlying curated JSON drifts again

If a future edit to `src/indexer/spec_data/cli_flags_<version>.json` (or a new Odoo point
release changing a chosen flag's real source) breaks parity, the CI-layer test goes RED
**for the right reason** - the curated data now disagrees with the committed, real-source
fixture. Fix the curated data (never loosen the assertion or the exclusion list to reach
green). If instead the fixture itself has gone stale (real source moved), regenerate the
fixture + this table + the test file's `_CHOSEN_FIXTURE_FLAGS` table together, in the same
commit, per the "Selection rule" above - never silently.
