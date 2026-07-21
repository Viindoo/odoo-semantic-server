# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_flags_content_parity.py
"""Content parity for spec_data/cli_flags_<version>.json vs the live oracle (issue #364 S3).

WHY THIS FILE EXISTS
---------------------
Two guards already exist for the cli_flags curated family and both stop short of content:

  - `tests/test_spec_data_cli_flags_curated.py` (719 lines) is a hardcoded regression
    snapshot of a past manual audit (issues #336/#338) - every assertion is a
    human-chosen expected value with a source `file:line` comment left as EVIDENCE the
    audit was done, not as a live check. If a fact was wrong when the test was written,
    the test enshrines the wrong fact forever.
  - `tests/test_parser_cli.py`'s WI-D2 section (issue #364 D2) wakes the live oracle
    (`parse_cli_flags`/`parse_cli_commands`) across all 12 surveyed majors, but
    DELIBERATELY only checks EXISTENCE (a non-empty parse) - see that section's own
    "SCOPE NOTE" pointing here for "a separate, dedicated content-parity effort".

This file is that effort. An audit (`/tmp/osm-364/phase2-A-cli-flags.md`, issue #364)
mechanically diffed every curated GLOBAL flag against a real AST parse of that version's
`tools/config.py` and found real, source-verified defects at scale: `--load`'s default
truncated at the first comma in 11/12 versions, ~15-19 flags/version with a fabricated
`type` that has no `type=` kwarg in source at all, help text bled from a neighbouring flag
onto 2-3 others (present in all 12 files including the freshly-redone v19), and `--debug`
in v8.0/v9.0 mismarked `deprecated` with a fabricated `replacement_flag_name` pointing at a
flag that does not even exist at v8 - a v10+ fact bled backward, the same shape as #362.

IF THIS FILE IS EVER MADE TO SKIP EVERYWHERE OR ITS ASSERTIONS WEAKENED TO REACH GREEN, IT
LOSES ITS ENTIRE JUSTIFICATION. This commit intentionally ships with the primary
(dev-box) layer FAILING - the curated data genuinely disagrees with the live oracle at
scale today. Making it pass here would mean either the exclusion list quietly grew to hide
a real field, or an assertion was loosened - both banned by the work item this file
implements. The follow-up data-correction commit is what should turn this green, not an
edit to this file.

SCOPE - GLOBAL ("server") FLAGS ONLY, BY DESIGN
------------------------------------------------
`parse_cli_flags()` / `_parse_options_calls()` (the live oracle) read ONLY
`<pkg>/tools/config.py`. They never open `cli/deploy.py`, `cli/db.py`, `cli/i18n.py`,
`cli/module.py`, `cli/scaffold.py`, `cli/cloc.py`, `cli/genproxytoken.py`,
`cli/upgrade_code.py` for their own argparse/optparse flags - there is no second walker in
production. Every per-command curated entry (`command_name` NOT in `{None, "server"}` -
10 to 86 entries per version, the MAJORITY of v19's 165-entry file) therefore has ZERO
production oracle at any surveyed version. This is not something a smarter comparison can
paper over: there is nothing to diff against. Rather than let those entries silently "pass"
by omission, `test_out_of_scope_per_command_flag_count_is_computed_and_reported_per_version`
below makes the count of unverifiable entries an explicit, asserted, per-version fact -
visible in the test's own output, not hidden inside a scope comment nobody reads.

FIELDS COMPARED vs FIELDS EXCLUDED (read this before touching either set)
----------------------------------------------------------------------------
`cli_flag.schema.json` declares 9 properties. `flag_name` is the match key (not a
"compared field" - it decides WHICH curated/oracle records get diffed at all).
`command_name` is the scope key (see "SCOPE" above): curated global flags use
`command_name: null`, `_load_static_cli_flags` coalesces that to `"server"` before it ever
reaches production (`test_load_static_cli_flags_coalesces_null_command_to_server` already
guards this), and the oracle always emits `command_name="server"` for config.py-sourced
flags (`parse_cli_flags` calls `_parse_options_calls(src, odoo_version,
command_name="server", ...)`). Once both sides are scoped to "global" they are constant by
construction - a per-field diff of `command_name` would only ever compare `"server"` to
itself, so it is not one of the 4 COMPARED_FIELDS below, for a different reason than the
three EXCLUDED_FIELDS.

COMPARED_FIELDS = {status, default, type, help} - the oracle's `_build_flag_info()`
(`parser_cli.py`, the ONE function both the AST tier and the SyntaxError text-fallback tier
call) derives all four directly from the real `add_option`/`add_argument` call's keyword
arguments, on every version, including v8/v9/v10 via the text-regex fallback (issue #364
A1). `default`/`type` legitimately return `None` for some real, correct options (no
`default=`/`type=` kwarg in source at all, or a non-`ast.Constant` value like a list/dict
literal or `_get_default_datadir()` the extractor cannot resolve) - that is a REAL,
reportable fact about what the oracle can see today, not grounds to exclude the field: the
audit's own mechanical pass (phase2-A-cli-flags.md S3a) treats these the same way and
defers the "is this specific mismatch a curator error or a defensible transcription of an
unresolvable expression" judgment call to the follow-up data-correction commit, which is
exactly where that human triage belongs - baking it into this test would mean silently
picking winners inside the alarm itself.

EXCLUDED_FIELDS (structural - the oracle NEVER populates these, confirmed by direct call,
not assumed from the schema) - see the `EXCLUDED_FIELDS` dict below for the full reasoning
per field. In one sentence: `replacement_flag_name`/`env_name`/`posix_only` all stay at
their `CLIFlagInfo` dataclass default (`None`/`None`/`False`) for EVERY flag the oracle
ever produces, regardless of what real source contains - comparing them would be comparing
curated data against a constant, which is exactly the "over-broad comparison that produces
false failures" this work item warned against. `since_version` - named as a for-instance in
this work item's brief - is NOT actually a property of `cli_flag.schema.json` /
`CLIFlagInfo` at all (grepped both; the only `since_version` in this repo belongs to an
unrelated curated family, `src/data/ee_modules.py`); it is recorded in `EXCLUDED_FIELDS`
purely so a reader who came here looking for it finds an explicit answer, not a silent gap.

`test_schema_properties_are_fully_accounted_for_by_compared_or_excluded_sets` below
machine-enforces that this partition (match key + scope key + COMPARED_FIELDS +
EXCLUDED_FIELDS) covers every property `cli_flag.schema.json` declares, so a future schema
edit cannot silently add a field this file never routes anywhere.

TWO LAYERS (ADR-0054 shape, per the #363 template `test_framework_bases_parity.py`)
----------------------------------------------------------------------------------------
1. CI layer (never skips) - `test_ci_excerpt_*` below, backed by
   `tests/fixtures/cli_flags_config_excerpts/`. See that directory's README.md for the
   fixture-cost measurement (482 KB full-file / 180 KB calls-only vs the 76 KB
   `odoo_tests_headers/` precedent for a DIFFERENT, much smaller curated family) and why a
   faithful per-version `config.py` fixture was rejected as disproportionate: unlike
   `framework_bases()`, where irrelevant method bodies can be reduced to `pass`, a CLI
   flag's `add_option()` call IS the fact under test end to end - there is no reducible
   filler inside it. What is committed instead: a small, real, verbatim (never
   hand-transcribed) 5-flag "confirmed mismatching" + 1-flag "healthy control" excerpt per
   version, ~1.4-1.9 KB each, 18.8 KB total - same size class as the `odoo_tests_headers/`
   precedent. This proves the diff machinery is real and currently catches genuine defects,
   on every CI run, with zero dependency on a checkout - not "something total" (that is the
   dev-box layer's job) but "something real and small, always on."
2. Dev-box layer (`test_curated_global_cli_flags_match_real_checkout_field_by_field`) - the
   full, un-excerpted comparison against a real `<pkg>/tools/config.py` checkout via
   `tests/_odoo_checkouts.checkout_root()`. THIS is where the audit's full-scale finding
   (roughly 70-98 mismatches per version, 967 across all 12 on this dev box - see the
   module-level report this file's own maintainers ran) actually surfaces. Marked
   `@pytest.mark.odoo_source`; skips per version when the checkout is absent, same
   discovery module `tests/test_parser_cli.py`'s WI-D2 section already unified on (issue
   #364 D2) - never a second, third convention for "where is the checkout."
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.indexer.models import CLIFlagInfo
from src.indexer.parser_cli import _parse_options_calls
from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root

SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
SCHEMA_PATH = SPEC_DATA_DIR / "cli_flag.schema.json"
CI_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cli_flags_config_excerpts"

# --- Fields compared vs excluded - see module docstring for the full reasoning. ----------

COMPARED_FIELDS: tuple[str, ...] = ("status", "default", "type", "help")

EXCLUDED_FIELDS: dict[str, str] = {
    "replacement_flag_name": (
        "Cross-version fact by construction (points at a successor flag that, when it "
        "exists, lives in a LATER version's file) - a single version's source has no way "
        "to know its own future successor. The only mechanical route to this fact is a "
        "cross-version diff (compute_cli_flag_diff, already exists in parser_cli.py) fed "
        "back into the JSON, which parse_cli_flags/_parse_options_calls do not do today. "
        "Confirmed by direct call: _build_flag_info() never reads or sets "
        "replacement_flag_name - CLIFlagInfo.replacement_flag_name stays at its dataclass "
        "default (None) for every flag the AST/text oracle produces, for every version, "
        "regardless of real source content."
    ),
    "env_name": (
        "Real source DOES carry this as a literal add_option kwarg from v19 (confirmed: "
        "group.add_option('-c', '--config', ..., env_name='ODOO_RC', ...) in "
        "odoo19/odoo/tools/config.py), and _extract_kwargs_strings() generically captures "
        "ANY ast.Constant kwarg including env_name - but _build_flag_info() (the one "
        "function both oracle tiers call to build a CLIFlagInfo) never reads "
        "kwargs['env_name'] into the object it returns. Verified directly: "
        "parse_cli_flags(<v19 checkout>, '19.0') on --config returns env_name=None even "
        "though real source sets one. This is a structural oracle gap, not a "
        "source-representation gap - every comparison would be curated-value-vs-None, "
        "100% false positives."
    ),
    "posix_only": (
        "Same shape as env_name: no code path in _build_flag_info ever sets it - "
        "CLIFlagInfo.posix_only stays at its dataclass default (False) for every "
        "oracle-produced flag. Separately (not the reason for exclusion, but worth "
        "recording): v19 wraps several real posix-only options in a PosixOnlyOption(...) "
        "call - group.add_option(PosixOnlyOption('--workers', ...)) - which "
        "_is_option_call/_flag_name_from_args cannot see AT ALL, because the outer "
        "add_option's only positional arg is a Call node, not a string ast.Constant. "
        "Confirmed by direct run: --workers, --limit-memory-hard, "
        "--limit-memory-hard-gevent, --limit-memory-soft-gevent, --limit-request and "
        "--limit-time-cpu all vanish from the oracle's v19 output entirely (not merely "
        "this field) even though every one is real, current source. Those flags never "
        "reach the matched set this file diffs, which is exactly why curated_only is "
        "reported but never asserted on below - a new, previously-undocumented oracle "
        "blind spot, distinct from the env_name/posix_only field-population gap."
    ),
}

# command_name and flag_name are handled separately (scope key / match key - see module
# docstring "COMPARED_FIELDS vs EXCLUDED_FIELDS"), not folded into either dict above.
_SCOPE_AND_MATCH_KEYS = {"flag_name", "command_name"}

# NOT a schema property - kept as its OWN constant (never merged into EXCLUDED_FIELDS,
# which is strictly "real schema properties the oracle structurally can't verify") so
# test_schema_properties_are_fully_accounted_for_by_compared_or_excluded_sets stays a
# faithful 1:1 partition of the real 9 schema properties, never silently padded with an
# entry that would make that guard pass for the wrong reason.
NOT_A_SCHEMA_FIELD: dict[str, str] = {
    "since_version": (
        "NOT a property of cli_flag.schema.json or a field of CLIFlagInfo - grepped both, "
        "zero hits. This work item's brief named it as a for-instance of an "
        "unverifiable field, generalizing from a DIFFERENT curated family "
        "(framework_bases-style 'since'/vintage facts elsewhere in the repo, e.g. "
        "src/data/ee_modules.py's unrelated since_version column). Recorded here only so "
        "a reader who came looking for it finds an explicit answer instead of a silent "
        "gap - see test_since_version_is_confirmed_absent_from_the_cli_flag_schema for "
        "the machine-checked proof."
    ),
}


@dataclass(frozen=True)
class FieldMismatch:
    flag_name: str
    field_name: str
    curated: object
    oracle: object


@dataclass(frozen=True)
class DiffResult:
    matched: frozenset[str]
    field_mismatches: tuple[FieldMismatch, ...]
    # Present in curated global set, absent from oracle output. NOT asserted on (see
    # module docstring): can mean a genuinely removed flag (curated correctly says so),
    # OR a real oracle blind spot (confirmed: the geoip-db multi-alias-per-call case and
    # the v19 PosixOnlyOption(...) wrapper case both produce this bucket for CORRECT
    # curated data) - conflating those two would produce exactly the false failures this
    # work item warned against.
    curated_only: frozenset[str]
    # Present in oracle output (i.e. demonstrably real, current source), absent from the
    # curated global set entirely. Unlike curated_only this has no legitimate "curator was
    # right" explanation - it means the curated JSON does not even have an entry for a
    # flag that provably exists. IS asserted on (see test below) - e.g. --log-config is
    # real at v17/v18/v19 (confirmed: odoo{17,18,19}/odoo/tools/config.py) and absent from
    # all three curated files.
    oracle_only: frozenset[str]

    def field_mismatch_counts(self) -> dict[str, int]:
        counts = {f: 0 for f in COMPARED_FIELDS}
        for m in self.field_mismatches:
            counts[m.field_name] += 1
        return counts


# --- Loaders -----------------------------------------------------------------------------

def _load_curated_flags(version: str, spec_dir: Path = SPEC_DATA_DIR) -> list[dict]:
    data = json.loads((spec_dir / f"cli_flags_{version}.json").read_text(encoding="utf-8"))
    return data.get("flags", [])


def _load_curated_global_flags(version: str, spec_dir: Path = SPEC_DATA_DIR) -> dict[str, dict]:
    """Curated flags scoped to 'global' (command_name in {None, 'server'}), keyed by
    flag_name - the SAME scoping the production merge applies (see module docstring
    "SCOPE")."""
    return {
        f["flag_name"]: f
        for f in _load_curated_flags(version, spec_dir)
        if f.get("command_name") in (None, "server")
    }


def _load_out_of_scope_count(version: str, spec_dir: Path = SPEC_DATA_DIR) -> int:
    """Count of curated per-command flags (command_name NOT in {None, 'server'}) - these
    have zero production oracle at any version (module docstring "SCOPE")."""
    return sum(
        1 for f in _load_curated_flags(version, spec_dir)
        if f.get("command_name") not in (None, "server")
    )


def _oracle_global_flags(
    source: str, version: str, file_path: str | None = None,
) -> dict[str, CLIFlagInfo]:
    """Run the production oracle (_parse_options_calls: AST tier, or the text-regex
    SyntaxError fallback for v8/v9/v10 - issue #364 A1) over *source*, keyed by
    flag_name. Every flag it returns for a config.py-sourced call is command_name="server"
    by construction (see module docstring)."""
    flags = _parse_options_calls(source, version, command_name="server", file_path=file_path)
    return {f.flag_name: f for f in flags}


def _diff_global_flags(curated: dict[str, dict], oracle: dict[str, CLIFlagInfo]) -> DiffResult:
    """The ONE comparison function both layers below call - field-by-field, over
    COMPARED_FIELDS only, for flags present on both sides."""
    curated_names = frozenset(curated)
    oracle_names = frozenset(oracle)
    matched = curated_names & oracle_names
    mismatches: list[FieldMismatch] = []
    for name in sorted(matched):
        c, o = curated[name], oracle[name]
        for field_name in COMPARED_FIELDS:
            curated_value = (
                c.get("status", "stable") if field_name == "status" else c.get(field_name)
            )
            oracle_value = getattr(o, field_name)
            if curated_value != oracle_value:
                mismatches.append(FieldMismatch(name, field_name, curated_value, oracle_value))
    return DiffResult(
        matched=matched,
        field_mismatches=tuple(mismatches),
        curated_only=curated_names - oracle_names,
        oracle_only=oracle_names - curated_names,
    )


def _format_diff_report(version: str, result: DiffResult, out_of_scope_count: int) -> str:
    counts = result.field_mismatch_counts()
    lines = [
        f"v{version}: {len(result.matched)} global flag(s) matched, "
        f"{len(result.field_mismatches)} field mismatch(es) "
        f"(by field: {counts}), "
        f"{len(result.oracle_only)} curated-coverage-gap(s) "
        f"(real per source, absent from curated global set: {sorted(result.oracle_only)}), "
        f"{len(result.curated_only)} curated-only (removed-or-oracle-blind-spot, not "
        f"asserted: {sorted(result.curated_only)}), "
        f"{out_of_scope_count} per-command flag(s) out of scope (no oracle exists - see "
        "module docstring 'SCOPE').",
    ]
    for m in sorted(result.field_mismatches, key=lambda m: (m.flag_name, m.field_name)):
        lines.append(
            f"  {m.flag_name!r:30} {m.field_name:8} curated={m.curated!r:40} oracle={m.oracle!r}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Structural guards (always run - not the RED signal; these protect the exclusion list
# and the scope-counting logic from silent drift).
# ---------------------------------------------------------------------------------------

def test_schema_properties_are_fully_accounted_for_by_compared_or_excluded_sets():
    """Business rule: every property cli_flag.schema.json declares must be routed to
    exactly one of {match/scope key, COMPARED_FIELDS, EXCLUDED_FIELDS} - so a future
    schema edit (a new curated property) cannot silently go both uncompared and
    unexplained. Mirrors test_the_only_curated_entry_without_a_file_path_is_testcase in
    the #363 template (test_framework_bases_parity.py)."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_props = set(schema["properties"])
    accounted_for = _SCOPE_AND_MATCH_KEYS | set(COMPARED_FIELDS) | set(EXCLUDED_FIELDS)
    assert schema_props == accounted_for, (
        f"schema declares {sorted(schema_props)} but this file accounts for "
        f"{sorted(accounted_for)} - route the difference to COMPARED_FIELDS or "
        "EXCLUDED_FIELDS (with a reason), never leave it unrouted"
    )


def test_compared_and_excluded_field_sets_are_disjoint():
    """Business rule: a field cannot simultaneously be 'the oracle can verify this' and
    'the oracle structurally never populates this' - a future edit adding a field to both
    dicts would silently make the comparison self-contradictory."""
    overlap = set(COMPARED_FIELDS) & set(EXCLUDED_FIELDS)
    assert not overlap, f"field(s) in both COMPARED_FIELDS and EXCLUDED_FIELDS: {overlap}"


def test_since_version_is_confirmed_absent_from_the_cli_flag_schema():
    """Business rule: `since_version` (named in this work item's brief as an example of
    an unverifiable field) must not silently be treated as a real, excluded CLIFlag
    property - it is not a property of this schema at all. Machine-checked so a future
    schema edit that DID add a since_version property would be caught here (at which
    point it should move into EXCLUDED_FIELDS proper, with its own oracle-behavior
    justification, not stay in NOT_A_SCHEMA_FIELD)."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "since_version" not in schema["properties"], (
        "since_version now IS a cli_flag.schema.json property - move it from "
        "NOT_A_SCHEMA_FIELD into EXCLUDED_FIELDS (or COMPARED_FIELDS) with a real "
        "oracle-behavior justification, and update the module docstring"
    )
    assert set(NOT_A_SCHEMA_FIELD) == {"since_version"}, (
        "NOT_A_SCHEMA_FIELD grew a second entry - each one needs the same "
        "confirmed-absent guard this test provides, not a shared assumption"
    )


@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_out_of_scope_per_command_flag_count_is_computed_and_reported_per_version(major):
    """Business rule: the per-command flag count (module docstring 'SCOPE' - flags with
    NO production oracle at any version) must be a visible, machine-checked fact per
    version, never a silent gap. Guards against the scope split itself drifting: global +
    out-of-scope must always equal the file's total flag count (a bug in the {None,
    "server"} filter would otherwise silently misclassify flags into the wrong bucket
    without any test noticing).
    """
    version = f"{major}.0"
    total = len(_load_curated_flags(version))
    global_count = len(_load_curated_global_flags(version))
    out_of_scope = _load_out_of_scope_count(version)
    assert global_count + out_of_scope == total, (
        f"v{major}: global ({global_count}) + out-of-scope ({out_of_scope}) != "
        f"total ({total}) - the command_name scope filter misclassified something"
    )
    # Visible in test output regardless of -q/-v (mirrors
    # test_cli_live_parser_coverage_is_reported in test_parser_cli.py).
    import warnings
    warnings.warn(
        UserWarning(
            f"cli_flags content-parity scope v{major}.0: {global_count} global flag(s) "
            f"in scope (oracle-covered), {out_of_scope} per-command flag(s) OUT of scope "
            "(no oracle exists in production - see module docstring 'SCOPE')."
        ),
        stacklevel=1,
    )


# ---------------------------------------------------------------------------------------
# Layer 1 - CI (must NEVER skip). See tests/fixtures/cli_flags_config_excerpts/README.md
# for the fixture-cost measurement and the small-real-excerpt design it led to.
# ---------------------------------------------------------------------------------------

# Flags each fixture was built to reproduce a known curated-vs-oracle mismatch on today
# (file order, first 5 mismatching + 1 healthy control - see the fixture README's
# "Selection rule"). Hardcoded here (not re-derived from a live full-file parse) so this
# layer never needs a checkout to know what it expects.
_CHOSEN_MISMATCH_FLAGS: dict[int, tuple[str, ...]] = {
    8: ("--addons-path", "--auto-reload", "--data-dir", "--database", "--db_host"),
    9: ("--addons-path", "--data-dir", "--database", "--db_host", "--db_password"),
    10: ("--addons-path", "--data-dir", "--database", "--db_host", "--db_password"),
    11: ("--addons-path", "--data-dir", "--database", "--db-filter", "--db_host"),
    12: ("--addons-path", "--data-dir", "--database", "--db-filter", "--db_host"),
    13: ("--addons-path", "--data-dir", "--database", "--db-filter", "--db_host"),
    14: ("--addons-path", "--data-dir", "--database", "--db-filter", "--db_host"),
    15: ("--addons-path", "--data-dir", "--database", "--db-filter", "--db_host"),
    16: ("--data-dir", "--database", "--db-filter", "--db_host", "--db_password"),
    17: ("--data-dir", "--database", "--db-filter", "--db_host", "--db_maxconn_gevent"),
    18: ("--data-dir", "--database", "--db-filter", "--db_host", "--db_maxconn_gevent"),
    19: ("--addons-path", "--data-dir", "--database", "--db-filter", "--db_app_name"),
}
_CHOSEN_HEALTHY_FLAG: dict[int, str] = {
    8: "--cert-file", 9: "--config", 10: "--config", 11: "--config", 12: "--config",
    13: "--config", 14: "--config", 15: "--config", 16: "--addons-path",
    17: "--addons-path", 18: "--addons-path", 19: "--config",
}


def _ci_excerpt_diff(major: int) -> DiffResult:
    version = f"{major}.0"
    fixture_path = CI_FIXTURE_DIR / f"v{major}_config_excerpt.py"
    assert fixture_path.is_file(), (
        f"no committed CI fixture for v{major} at {fixture_path} - the CI layer must "
        "fail loudly on a missing fixture, never silently skip (mirrors "
        "test_framework_bases_parity.py's module docstring contract)"
    )
    source = fixture_path.read_text(encoding="utf-8")
    curated = _load_curated_global_flags(version)
    fixture_name_groups = (_CHOSEN_MISMATCH_FLAGS[major], (_CHOSEN_HEALTHY_FLAG[major],))
    fixture_flags = {n for names in fixture_name_groups for n in names}
    curated_scoped = {k: v for k, v in curated.items() if k in fixture_flags}
    oracle = _oracle_global_flags(source, version, file_path=str(fixture_path))
    return _diff_global_flags(curated_scoped, oracle)


def test_ci_fixture_dir_has_one_excerpt_per_surveyed_major():
    """CI layer coverage guard - business rule: this file's parametrization must
    exercise the full [8, 19] surveyed range. Mirrors
    test_ci_layer_covers_every_surveyed_major_with_no_silent_gaps in the #363 template."""
    assert SURVEYED_MAJORS == list(range(8, 20))
    fixture_majors = {
        int(p.stem.split("_")[0][1:]) for p in CI_FIXTURE_DIR.glob("v*_config_excerpt.py")
    }
    assert fixture_majors == set(SURVEYED_MAJORS), (
        f"expected a v<major>_config_excerpt.py fixture for every major in "
        f"{SURVEYED_MAJORS}, found fixtures for {sorted(fixture_majors)}"
    )


@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_ci_excerpt_reproduces_confirmed_curated_global_flag_mismatch_per_version(major):
    """CI layer (never skips, no checkout needed) - business rule: for the small,
    real, verbatim per-version excerpt, every flag chosen as 'confirmed mismatching' at
    fixture-build time must still disagree with curated data on at least one of
    status/default/type/help. Expected RED today (issue #364) - this is what proves the
    diff machinery is real and currently exercised on every CI run, not fixture-only
    theater with nothing to disagree on. If this ever goes green it means the underlying
    spec_data/*.json was corrected (good!) - regenerate the fixture + _CHOSEN_* tables +
    this file's README together, per the fixture README's last section, rather than
    treating a pass here as this test being broken.
    """
    result = _ci_excerpt_diff(major)
    mismatched_flags = {m.flag_name for m in result.field_mismatches}
    expected = set(_CHOSEN_MISMATCH_FLAGS[major])
    still_matching_curated = expected - mismatched_flags
    assert not still_matching_curated, (
        f"v{major}: expected {sorted(expected)} to still disagree with curated "
        f"cli_flags_{major}.0.json on >=1 field - {sorted(still_matching_curated)} now "
        f"match. Full diff: {_format_diff_report(f'{major}.0', result, 0)}"
    )


@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_ci_excerpt_healthy_control_flag_has_zero_mismatch_per_version(major):
    """CI layer negative control - business rule: the one flag chosen as a 'healthy'
    (zero-mismatch) control per version must stay a genuine agreement, proving this
    comparison does not just flag every flag unconditionally - a comparison that always
    fails is exactly as useless as one that never does.
    """
    result = _ci_excerpt_diff(major)
    healthy_name = _CHOSEN_HEALTHY_FLAG[major]
    healthy_mismatches = [m for m in result.field_mismatches if m.flag_name == healthy_name]
    assert not healthy_mismatches, (
        f"v{major}: healthy control {healthy_name!r} was expected to match curated data "
        f"exactly - got mismatches {healthy_mismatches}"
    )


# ---------------------------------------------------------------------------------------
# Layer 2 - dev-box (real checkouts; skips per version when absent, never fails).
# THIS is the layer that reproduces the audit's full-scale finding. Expect RED here on
# any machine with real Odoo checkouts on disk - see module docstring.
# ---------------------------------------------------------------------------------------

@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_global_cli_flags_match_real_checkout_field_by_field(major):
    """Dev-box layer - business rule: for every surveyed major with a real checkout on
    this machine, every curated GLOBAL cli flag's status/default/type/help must equal
    what the production oracle derives from a live parse of the real, full,
    un-excerpted tools/config.py, AND every flag the oracle can prove exists in real
    source must have a curated entry at all (oracle_only must be empty). Expected RED
    today (issue #364) - see module docstring for the confirmed defect classes this
    reproduces (truncated --load default, fabricated type, help-text bleed, mismarked
    --debug deprecation at v8/v9, plus a missing --log-config entry at v17-v19 this
    audit had not previously named).
    """
    version = f"{major}.0"
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS to override)")
    from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
    pkg = "openerp" if major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR else "odoo"
    config_path = root / pkg / "tools" / "config.py"
    if not config_path.is_file():
        pytest.skip(f"{config_path} not found")
    source = config_path.read_text(encoding="utf-8", errors="ignore")
    curated = _load_curated_global_flags(version)
    oracle = _oracle_global_flags(source, version, file_path=str(config_path))
    result = _diff_global_flags(curated, oracle)
    out_of_scope = _load_out_of_scope_count(version)
    report = _format_diff_report(version, result, out_of_scope)
    assert not result.field_mismatches and not result.oracle_only, report


def test_dev_box_content_parity_coverage_is_reported():
    """Coverage visibility - business rule: a per-version skip in the dev-box layer must
    announce itself in aggregate, not just fade into individual SKIPPED lines nobody
    reads (the same "guard that doesn't guard" failure issue #364 D2 fixed for
    existence-only checks). Mirrors test_cli_live_parser_coverage_is_reported in
    test_parser_cli.py.
    """
    import warnings
    assert SURVEYED_MAJORS == list(range(8, 20))
    exercised = [m for m in SURVEYED_MAJORS if checkout_root(m) is not None]
    warnings.warn(
        UserWarning(
            "cli_flags content-parity dev-box coverage: "
            f"{len(exercised)}/{len(SURVEYED_MAJORS)} surveyed majors have a checkout on "
            f"this machine (exercised: {exercised or 'NONE'}). Set OSM_ODOO_CHECKOUTS to "
            "point at your checkouts if this reads 0."
        ),
        stacklevel=1,
    )
