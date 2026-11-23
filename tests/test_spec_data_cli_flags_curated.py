# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for curated cli_flags_<version>.json spec data files (WI-A5).

Validates:
- Every surveyed version (8.0 to 20.0) has a file with _curate_status="complete" and
  len(flags) >= 20
- Every flag entry conforms to the cli_flag.schema.json schema
- (``odoo_source``, every surveyed major) the per-command flags, the sub-actions and the
  curated-only fields of the global flags (``env_name``, ``posix_only``,
  ``replacement_flag_name``) agree with the real odoo/odoo source of that major
"""
import ast
import json
import re
from pathlib import Path

import pytest

from src.indexer.parser_cli import (
    _NOT_CONSTANT,
    _constant_value,
    _find_option_call_arg_spans,
    _parse_options_calls,
    _pkg_prefix,
)
from src.indexer.parser_util import parse_external_source
from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root

SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
SCHEMA_PATH = SPEC_DATA_DIR / "cli_flag.schema.json"

# Every surveyed version must be curated (SSOT: src.constants.SURVEYED_MAJORS).
CURATED_VERSIONS = [f"{major}.0" for major in SURVEYED_MAJORS]

MIN_FLAGS_PER_VERSION = 20

VALID_STATUSES = {"stable", "deprecated", "removed"}


def _load_version(version: str) -> dict:
    path = SPEC_DATA_DIR / f"cli_flags_{version}.json"
    assert path.is_file(), f"Missing spec data file: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _load_schema() -> dict:
    assert SCHEMA_PATH.is_file(), f"Missing schema file: {SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class TestEachVersionHasCuratedStatusComplete:
    """Every surveyed version's cli_flags_*.json must be marked complete with >= 20 flags."""

    def test_curated_range_is_the_full_surveyed_range(self) -> None:
        # Pins the range against silent narrowing: dropping a version from the SSOT
        # would otherwise shrink every parametrized guard in this file without a failure.
        assert CURATED_VERSIONS == [f"{major}.0" for major in range(8, 21)]

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_curate_status_is_complete(self, version: str) -> None:
        data = _load_version(version)
        assert data.get("_curate_status") == "complete", (
            f"cli_flags_{version}.json: _curate_status is "
            f"{data.get('_curate_status')!r}, expected 'complete'"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_minimum_flag_count(self, version: str) -> None:
        # Absorbs the former `test_flags_key_is_list`: the `flags` key must be a
        # list AND carry >= MIN_FLAGS_PER_VERSION entries. Both assertions kept.
        data = _load_version(version)
        flags = data.get("flags")
        assert isinstance(flags, list), (
            f"cli_flags_{version}.json: 'flags' must be a list"
        )
        assert len(flags) >= MIN_FLAGS_PER_VERSION, (
            f"cli_flags_{version}.json: only {len(flags)} flags, "
            f"expected >= {MIN_FLAGS_PER_VERSION}"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_server_command_always_present(self, version: str) -> None:
        """Every Odoo version must have a 'server' command.

        Absorbs the former `test_commands_key_present`: the `commands` key must
        be present AND a list (pre-condition), then 'server' must appear in it.
        All three assertions kept.
        """
        data = _load_version(version)
        assert "commands" in data, (
            f"cli_flags_{version}.json: 'commands' key missing"
        )
        assert isinstance(data["commands"], list), (
            f"cli_flags_{version}.json: 'commands' must be a list"
        )
        command_names = {c.get("name") for c in data["commands"]}
        assert "server" in command_names, (
            f"cli_flags_{version}.json: 'server' command missing from commands list"
        )


class TestFlagSchemaValid:
    """Every flag entry must satisfy the cli_flag.schema.json schema."""

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_all_flags_have_required_fields(self, version: str) -> None:
        data = _load_version(version)
        for i, flag in enumerate(data.get("flags", [])):
            assert "flag_name" in flag, (
                f"cli_flags_{version}.json flags[{i}]: missing 'flag_name'"
            )
            assert "command_name" in flag, (
                f"cli_flags_{version}.json flags[{i}]: missing 'command_name'"
            )
            assert "status" in flag, (
                f"cli_flags_{version}.json flags[{i}]: missing 'status'"
            )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_flag_name_starts_with_dash(self, version: str) -> None:
        data = _load_version(version)
        for i, flag in enumerate(data.get("flags", [])):
            fname = flag.get("flag_name", "")
            assert fname.startswith("-"), (
                f"cli_flags_{version}.json flags[{i}]: flag_name {fname!r} "
                f"must start with '-'"
            )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_status_values_valid(self, version: str) -> None:
        data = _load_version(version)
        for i, flag in enumerate(data.get("flags", [])):
            status = flag.get("status")
            assert status in VALID_STATUSES, (
                f"cli_flags_{version}.json flags[{i}] ({flag.get('flag_name')}): "
                f"invalid status {status!r}, must be one of {sorted(VALID_STATUSES)}"
            )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_replacement_flag_name_type(self, version: str) -> None:
        """replacement_flag_name must be str or null."""
        data = _load_version(version)
        for i, flag in enumerate(data.get("flags", [])):
            rfn = flag.get("replacement_flag_name")
            assert rfn is None or isinstance(rfn, str), (
                f"cli_flags_{version}.json flags[{i}] ({flag.get('flag_name')}): "
                f"replacement_flag_name must be str or null, got {type(rfn).__name__}"
            )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_posix_only_is_boolean(self, version: str) -> None:
        data = _load_version(version)
        for i, flag in enumerate(data.get("flags", [])):
            po = flag.get("posix_only")
            if po is not None:
                assert isinstance(po, bool), (
                    f"cli_flags_{version}.json flags[{i}] ({flag.get('flag_name')}): "
                    f"posix_only must be bool, got {type(po).__name__}"
                )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_no_duplicate_flag_names(self, version: str) -> None:
        # WI-B: dedup by (flag_name, command_name) tuple - a flag like --config
        # may appear under multiple commands (server, db, i18n) without being a
        # duplicate. Only a flag with the SAME name on the SAME command is a dup.
        data = _load_version(version)
        flags = data.get("flags", [])
        keys = [(f.get("flag_name"), f.get("command_name")) for f in flags]
        seen: set[tuple] = set()
        dups = []
        for key in keys:
            if key in seen:
                dups.append(key)
            seen.add(key)
        assert not dups, (
            f"cli_flags_{version}.json: duplicate (flag_name, command_name) pairs: {dups}"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_essential_flags_present(self, version: str) -> None:
        """Core flags that must exist in every Odoo version."""
        data = _load_version(version)
        flag_names = {f.get("flag_name") for f in data.get("flags", [])}
        essential = {"--addons-path", "--database", "--db_user", "--logfile"}
        missing = essential - flag_names
        assert not missing, (
            f"cli_flags_{version}.json: missing essential flags: {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# Real jsonschema-library validation (issue #364 A7)
# ---------------------------------------------------------------------------
# TestFlagSchemaValid above hand-re-implements a SUBSET of cli_flag.schema.json
# (required fields, flag_name prefix, status enum, replacement_flag_name/
# posix_only type) - it never checked `default`/`type`/`help`/`env_name`, so
# 11 real pre-existing violations (int where the schema declares string, e.g.
# `cloc --verbose` default=0, `genproxytoken --token-length` default=16, at
# v12.0-v18.0) shipped undetected for as long as this file existed. Worse,
# `_load_schema()` (above) was defined and never called anywhere - a schema
# file that is loaded but never validated against is decorative, not a gate.
# This class fixes both: it actually calls `_load_schema()` and runs the real
# `jsonschema` library (already a pinned dependency, pyproject.toml) against
# every property the schema declares, not just the ones a human remembered to
# re-check. Mirrors tests/test_patterns_schema.py's established pattern for
# patterns.schema.json. Existing TestFlagSchemaValid assertions are left
# unweakened - this is additive defense-in-depth, not a replacement.

class TestJsonschemaLibraryValidation:
    @pytest.fixture(scope="class")
    @classmethod
    def validator(cls):
        from jsonschema import validators
        schema = _load_schema()
        validator_cls = validators.validator_for(schema)
        validator_cls.check_schema(schema)
        return validator_cls(schema)

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_all_flags_validate_against_real_jsonschema(self, validator, version: str) -> None:
        data = _load_version(version)
        errors = []
        for i, flag in enumerate(data.get("flags", [])):
            for err in validator.iter_errors(flag):
                errors.append(f"flags[{i}] ({flag.get('flag_name')}): {err.message}")
        assert not errors, (
            f"cli_flags_{version}.json has jsonschema violations:\n" + "\n".join(errors)
        )

    def test_validator_actually_rejects_a_broken_record(self, validator) -> None:
        """A validator that cannot fail is the thing issue #364 A7 eliminates -
        prove this one can, on a record shaped exactly like the 11 real
        pre-existing violations this pass found (int `default` where the
        schema declares string)."""
        broken = {
            "flag_name": "--example",
            "command_name": "cloc",
            "status": "stable",
            "default": 0,  # schema requires ["string", "null"]
        }
        errors = list(validator.iter_errors(broken))
        assert errors, "expected the broken record (int default) to fail validation"


class TestV19SubparserCommands:
    """WI-D: Static guards verifying v19 subparser commands are fully indexed.

    These tests are written RED first (before WI-A fills the data) and are
    expected to turn GREEN once cli_flags_19.0.json is complete.
    """

    V19 = "19.0"

    # 13 sub-actions across the 3 subparser commands.
    EXPECTED_SUB_ACTIONS = {
        "i18n import", "i18n export", "i18n loadlang",
        "db init", "db load", "db dump", "db duplicate", "db rename", "db drop",
        "module install", "module upgrade", "module uninstall", "module force-demo",
    }

    # Representative flags confirmed-by-source in audit §3 (file:line).
    # These must exist with the EXACT (flag_name, command_name) pair.
    REPRESENTATIVE_FLAGS = [
        # i18n.py:75-83
        ("--language", "i18n import"),   # required (encoded in help text)
        # i18n.py:85-99
        ("--output", "i18n export"),
        # db.py:54-96
        ("--with-demo", "db init"),
        # module.py:84-91
        ("--outdated", "module upgrade"),
    ]

    def _flags_by_cmd(self, data: dict) -> dict[str, list[str]]:
        """Build {command_name: [flag_name, ...]} index from JSON data."""
        result: dict[str, list[str]] = {}
        for f in data.get("flags", []):
            cmd = f.get("command_name")
            if cmd:
                result.setdefault(cmd, []).append(f.get("flag_name"))
        return result

    def test_all_subparser_commands_indexed_v19(self) -> None:
        """3 subparser commands must have all 13 sub-actions in commands[]
        and each sub-action must appear as command_name on at least one flag.
        """
        data = _load_version(self.V19)
        command_names = {c.get("name") for c in data.get("commands", [])}
        flags_by_cmd = self._flags_by_cmd(data)

        missing_cmds = self.EXPECTED_SUB_ACTIONS - command_names
        assert not missing_cmds, (
            f"cli_flags_19.0.json: missing sub-action command entries: "
            f"{sorted(missing_cmds)}"
        )

        # Each sub-action must have at least one flag indexed under it.
        # (module force-demo only inherits common flags - the 3 common flags
        # --config/--database/--data-dir are indexed under "module", not under
        # "module force-demo"; force-demo itself has 0 extra flags, so this check
        # applies only to sub-actions that have extra flags.)
        sub_actions_with_own_flags = self.EXPECTED_SUB_ACTIONS - {"module force-demo"}
        missing_flags = [
            cmd for cmd in sub_actions_with_own_flags
            if cmd not in flags_by_cmd
        ]
        assert not missing_flags, (
            f"cli_flags_19.0.json: these sub-actions have no flags indexed: "
            f"{sorted(missing_flags)}"
        )

    def test_representative_flags_present_v19(self) -> None:
        """Spot-check confirmed-by-source flags from audit §3."""
        data = _load_version(self.V19)
        keys = {
            (f.get("flag_name"), f.get("command_name"))
            for f in data.get("flags", [])
        }
        missing = [
            f"{fn!r} on {cmd!r}" for fn, cmd in self.REPRESENTATIVE_FLAGS
            if (fn, cmd) not in keys
        ]
        assert not missing, (
            f"cli_flags_19.0.json: missing representative flags: {missing}"
        )

    def test_non_subparser_commands_have_flags_v19(self) -> None:
        """Every non-subparser command (except help) must have >= 1 flag entry.

        This catches regression where a command is listed in commands[] but has
        no flags indexed under its command_name.
        Commands that inherit server flags are expected to have at least their
        extra flags indexed separately (shell: 2, start: 2, populate: 3, etc.).
        module force-demo has only the 3 common flags on the parent 'module'
        command_name, zero extra flags of its own - excluded deliberately.
        """
        data = _load_version(self.V19)
        # Subparser parents delegate to compound sub-action names; help has no
        # own flags; module force-demo has no extra flags beyond parent 'module'.
        excluded = {"server", "help", "i18n", "db", "module", "module force-demo"}
        flags_by_cmd = self._flags_by_cmd(data)

        no_flags = []
        for cmd in data.get("commands", []):
            name = cmd.get("name", "")
            if name in excluded:
                continue
            if name not in flags_by_cmd:
                no_flags.append(name)

        assert not no_flags, (
            f"cli_flags_19.0.json: these non-subparser commands have no flags "
            f"indexed: {sorted(no_flags)}"
        )

    def test_no_stale_command_v19(self) -> None:
        """tsconfig was removed in v19 - must NOT appear as active in v19 JSON.

        Acceptable: absent entirely, or present with status='removed' on its flags.
        Unacceptable: present in commands[] with no 'removed' marker.
        """
        data = _load_version(self.V19)
        command_names = {c.get("name") for c in data.get("commands", [])}
        # tsconfig must be absent from the commands list (preferred), OR if listed
        # it must have at least one flag with status='removed' to signal the removal.
        if "tsconfig" in command_names:
            tsconfig_flags_statuses = [
                f.get("status")
                for f in data.get("flags", [])
                if f.get("command_name") == "tsconfig"
            ]
            has_removed = any(s == "removed" for s in tsconfig_flags_statuses)
            assert has_removed, (
                "cli_flags_19.0.json: 'tsconfig' is listed in commands[] but has "
                "no flag with status='removed'. Either remove it from commands[] "
                "or add a sentinel flag with status='removed'."
            )


class TestV20CliSurface:
    """Odoo 20.0 odoo-bin surface: the commands, sub-actions and flags 20.0 ships.

    Facts come from odoo/odoo branch 20.0 (odoo/cli/*.py, addons/*/cli/*.py,
    odoo/tools/config.py). The static guards run everywhere; the ``odoo_source``
    oracles of ``TestCommandFlagsMatchTheRealSource`` (every surveyed major) diff the
    curated per-command flags against the argparse/optparse declarations of a real checkout.
    """

    V20 = "20.0"

    EXPECTED_SUB_ACTIONS = {
        "i18n import", "i18n export", "i18n loadlang",
        "db init", "db load", "db dump", "db duplicate", "db rename", "db drop",
        "module install", "module upgrade", "module uninstall", "module force-demo",
        "module list",
    }

    def _command_names(self) -> set:
        return {c.get("name") for c in _load_version(self.V20)["commands"]}

    def _flags_of(self, command: str) -> dict:
        return {
            f["flag_name"]: f
            for f in _load_version(self.V20)["flags"]
            if f.get("command_name") == command
        }

    def _global(self, flag_name: str) -> dict:
        matches = [
            f for f in _load_version(self.V20)["flags"]
            if f.get("flag_name") == flag_name and f.get("command_name") is None
        ]
        assert len(matches) == 1, f"expected exactly one global {flag_name!r}, got {len(matches)}"
        return matches[0]

    def test_every_sub_action_is_a_command(self) -> None:
        missing = self.EXPECTED_SUB_ACTIONS - self._command_names()
        assert not missing, f"cli_flags_20.0.json: missing sub-action commands: {sorted(missing)}"

    def test_every_sub_action_with_own_flags_has_them(self) -> None:
        # module force-demo and module list take only the flags shared on "module".
        flagged = {f["command_name"] for f in _load_version(self.V20)["flags"]}
        without_flags = (self.EXPECTED_SUB_ACTIONS - {"module force-demo", "module list"}) - flagged
        assert not without_flags, f"sub-actions without flags: {sorted(without_flags)}"

    def test_every_command_other_than_the_pure_dispatchers_has_flags(self) -> None:
        excluded = {"server", "help", "i18n", "db", "module", "module force-demo", "module list"}
        flagged = {f["command_name"] for f in _load_version(self.V20)["flags"]}
        no_flags = sorted(self._command_names() - excluded - flagged)
        assert not no_flags, f"commands listed in commands[] without any flag: {no_flags}"

    def test_core_commands_follow_the_20_layout(self) -> None:
        by_name = {c["name"]: c for c in _load_version(self.V20)["commands"]}
        # duplicate is a core command; populate moved to the populate addon.
        assert by_name["duplicate"]["file_path"] == "odoo/cli/duplicate.py"
        assert by_name["populate"]["file_path"] == "addons/populate/cli/populate.py"
        assert by_name["genproxytoken"]["file_path"] == "addons/iot_drivers/cli/genproxytoken.py"
        assert "tsconfig" not in by_name

    def test_duplicate_and_populate_carry_their_own_flag_sets(self) -> None:
        assert set(self._flags_of("duplicate")) == {"--factors", "--models", "--sep"}
        assert set(self._flags_of("populate")) == {
            "--blueprint", "--seed", "--scale", "--jobs", "--resume", "--profile",
        }

    def test_module_sub_actions_take_dry_run_and_list_is_a_sub_action(self) -> None:
        for sub in ("module install", "module upgrade", "module uninstall"):
            assert "--dry-run" in self._flags_of(sub), f"{sub} must offer --dry-run"
        assert "--outdated" in self._flags_of("module upgrade")
        assert "module list" in self._command_names()

    def test_db_load_offers_move(self) -> None:
        assert "--move" in self._flags_of("db load")

    def test_skip_auto_install_is_a_global_server_flag(self) -> None:
        flag = self._global("--skip-auto-install")
        assert flag["status"] == "stable"
        assert flag["default"] == "False"

    def test_stop_and_stop_after_init_are_both_live_global_flags(self) -> None:
        # config.py declares one option under both names; neither is deprecated.
        for name in ("--stop", "--stop-after-init"):
            flag = self._global(name)
            assert flag["status"] == "stable" and flag["replacement_flag_name"] is None

    def test_flags_new_in_20_are_global(self) -> None:
        assert self._global("--unsafe-policy")["type"] == "choice"
        assert self._global("--db-system")["env_name"] == "PGDATABASE_SYSTEM"
        assert self._global("--gevent-workers")["posix_only"] is True

    def test_syslog_is_deprecated_in_favour_of_log_config(self) -> None:
        flag = self._global("--syslog")
        assert flag["status"] == "deprecated"
        assert flag["replacement_flag_name"] == "--log-config"
        assert "--log-config" in {f["flag_name"] for f in _load_version(self.V20)["flags"]}

    def test_http_interface_defaults_to_loopback(self) -> None:
        assert self._global("--http-interface")["default"] == "127.0.0.1"

    def test_posix_only_flags_are_exactly_the_posix_only_option_wrappers(self) -> None:
        posix = {
            f["flag_name"] for f in _load_version(self.V20)["flags"] if f.get("posix_only")
        }
        assert posix == {
            "--workers", "--gevent-workers", "--limit-memory-soft-gevent",
            "--limit-memory-hard", "--limit-memory-hard-gevent", "--limit-time-cpu",
            "--limit-request",
        }


class TestV18SpecCorrectness:
    """v18 spec must reflect the REAL odoo-bin CLI (verified against odoo18 source).

    Guards the correctness fix that removed 4 flags absent from
    odoo18/odoo/tools/config.py and added the upgrade_code command + 6 flags
    from odoo18/odoo/cli/upgrade_code.py. The test protects the BEHAVIOR
    "v18 spec mirrors the actual v18 CLI", not a frozen flag count.
    """

    V18 = "18.0"

    # These flags do NOT exist in odoo18/odoo/tools/config.py:
    #   --db_app_name / --reinit / --with-demo  -> v19-only additions
    #   --longpolling-port                      -> removed in v18 (alias in v16/v17)
    PHANTOM_FLAGS = {"--db_app_name", "--reinit", "--with-demo", "--longpolling-port"}

    # upgrade_code parser flags, from odoo18/odoo/cli/upgrade_code.py argparse.
    UPGRADE_CODE_FLAGS = {
        "--script", "--from", "--to", "--glob", "--dry-run", "--addons-path",
    }

    def test_phantom_flags_absent_v18(self) -> None:
        """The 4 v19-only / removed flags must NOT appear in the v18 spec."""
        data = _load_version(self.V18)
        present = {
            f.get("flag_name")
            for f in data.get("flags", [])
            if f.get("flag_name") in self.PHANTOM_FLAGS
        }
        assert not present, (
            f"cli_flags_18.0.json: flags absent from v18 source must be removed, "
            f"but found: {sorted(present)}"
        )

    def test_upgrade_code_command_indexed_v18(self) -> None:
        """upgrade_code command must be present in the v18 commands[]."""
        data = _load_version(self.V18)
        command_names = {c.get("name") for c in data.get("commands", [])}
        assert "upgrade_code" in command_names, (
            "cli_flags_18.0.json: 'upgrade_code' command missing from commands[] "
            "(exists at odoo18/odoo/cli/upgrade_code.py)"
        )

    def test_upgrade_code_flags_indexed_v18(self) -> None:
        """All 6 upgrade_code flags must be indexed under command_name='upgrade_code'."""
        data = _load_version(self.V18)
        uc_flags = {
            f.get("flag_name")
            for f in data.get("flags", [])
            if f.get("command_name") == "upgrade_code"
        }
        missing = self.UPGRADE_CODE_FLAGS - uc_flags
        assert not missing, (
            f"cli_flags_18.0.json: upgrade_code flags missing: {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# WI-1T: New test classes for issue #338 (CLI flag backfill v8-v18)
# Red-green discipline: these tests FAIL on base (data not yet backfilled)
# and turn GREEN once WI-1a through WI-1d complete the JSON entries.
# ---------------------------------------------------------------------------

# Required flags per argparse-backed command (long-form only; aliases excluded).
# Source: solution-338.md §3 "Per-Version Checklist".
_DEPLOY_REQUIRED: frozenset[str] = frozenset({
    "--path", "--url", "--db", "--login", "--password", "--verify-ssl", "--force",
})
_START_REQUIRED: frozenset[str] = frozenset({"--path", "--database"})
_SCAFFOLD_REQUIRED: frozenset[str] = frozenset({"--template"})
_CLOC_REQUIRED: frozenset[str] = frozenset({"--database", "--path", "--verbose"})
_GENPROXYTOKEN_REQUIRED: frozenset[str] = frozenset({"--token-length"})

# Version ranges per command (solution-338 §3 + overall-plan §1).
_V8_TO_V18 = ["8.0", "9.0", "10.0", "11.0", "12.0", "13.0", "14.0", "15.0", "16.0", "17.0", "18.0"]
_DEPLOY_VERSIONS = _V8_TO_V18
_START_VERSIONS = _V8_TO_V18
_SCAFFOLD_VERSIONS = _V8_TO_V18
_CLOC_VERSIONS = ["12.0", "13.0", "14.0", "15.0", "16.0", "17.0", "18.0"]
_GENPROXYTOKEN_VERSIONS = ["15.0", "16.0", "17.0", "18.0"]

# Flags that are EXCLUSIVELY per-command argparse flags (NOT re-declared in
# config.py as global flags). These must never appear with command_name=null.
# Excluded from this set: --path, --database, --url (also in config.py globals).
_EXCLUSIVE_PER_COMMAND_FLAGS: frozenset[str] = frozenset({
    # deploy.py only
    "--db", "--login", "--password", "--verify-ssl",
    # scaffold.py only
    "--template",
    # cloc.py only
    "--verbose",
    # genproxytoken.py only
    "--token-length",
})


class TestPerCommandFlagsCoverage:
    """Per-command argparse flags (Gap 1) must be present with correct command_name.

    Guards: cli_help('deploy', version=X) returns --db/--login/--password/
    --verify-ssl/--force; cli_help('cloc', ...) returns --path/--verbose/
    --database; etc. Fails on base because no per-command entries exist yet.
    """

    @pytest.mark.parametrize("version", _DEPLOY_VERSIONS)
    def test_deploy_flags_per_version(self, version: str) -> None:
        """deploy command has required flags with command_name='deploy' for all v8-18."""
        data = _load_version(version)
        present = {
            f["flag_name"]
            for f in data.get("flags", [])
            if f.get("command_name") == "deploy"
        }
        missing = _DEPLOY_REQUIRED - present
        assert not missing, (
            f"cli_flags_{version}.json: deploy command missing flags: {sorted(missing)}"
        )

    @pytest.mark.parametrize("version", _START_VERSIONS)
    def test_start_flags_per_version(self, version: str) -> None:
        """start command has --path and --database with command_name='start' for all v8-18.

        Note: both flags exist across all versions (v8/v9 source also has --database
        long-form); only the long-form is indexed per v19 convention.
        """
        data = _load_version(version)
        present = {
            f["flag_name"]
            for f in data.get("flags", [])
            if f.get("command_name") == "start"
        }
        missing = _START_REQUIRED - present
        assert not missing, (
            f"cli_flags_{version}.json: start command missing flags: {sorted(missing)}"
        )

    @pytest.mark.parametrize("version", _SCAFFOLD_VERSIONS)
    def test_scaffold_flags_per_version(self, version: str) -> None:
        """scaffold command has --template with command_name='scaffold' for all v8-18.

        v8/v9/v10 scaffold.py use Python-2 print syntax (AST parser returns 0 flags);
        --template must be hand-curated for those versions.
        """
        data = _load_version(version)
        present = {
            f["flag_name"]
            for f in data.get("flags", [])
            if f.get("command_name") == "scaffold"
        }
        missing = _SCAFFOLD_REQUIRED - present
        assert not missing, (
            f"cli_flags_{version}.json: scaffold command missing flags: {sorted(missing)}"
        )

    @pytest.mark.parametrize("version", _CLOC_VERSIONS)
    def test_cloc_flags_per_version(self, version: str) -> None:
        """cloc command (introduced in v12) has --database/--path/--verbose for v12-18."""
        data = _load_version(version)
        present = {
            f["flag_name"]
            for f in data.get("flags", [])
            if f.get("command_name") == "cloc"
        }
        missing = _CLOC_REQUIRED - present
        assert not missing, (
            f"cli_flags_{version}.json: cloc command missing flags: {sorted(missing)}"
        )

    @pytest.mark.parametrize("version", _GENPROXYTOKEN_VERSIONS)
    def test_genproxytoken_flags_per_version(self, version: str) -> None:
        """genproxytoken command (introduced in v15) has --token-length for v15-18.

        --config must NOT be indexed here (it bleeds from config.load() and is
        already indexed as a shared global with command_name=null).
        """
        data = _load_version(version)
        present = {
            f["flag_name"]
            for f in data.get("flags", [])
            if f.get("command_name") == "genproxytoken"
        }
        missing = _GENPROXYTOKEN_REQUIRED - present
        assert not missing, (
            f"cli_flags_{version}.json: genproxytoken command missing flags: "
            f"{sorted(missing)}"
        )

    @pytest.mark.parametrize("version", _DEPLOY_VERSIONS)
    def test_no_null_command_for_per_command_flags(self, version: str) -> None:
        """Exclusively per-command flags must not appear with command_name=null.

        Flags in _EXCLUSIVE_PER_COMMAND_FLAGS originate from argparse parsers in
        cli/*.py and are NOT re-declared in config.py. If any of them appears
        with command_name=null it was incorrectly indexed as a global flag.

        Shared flags (--path, --database, --url) are explicitly excluded because
        config.py also declares them as global options - having command_name=null
        for those is correct alongside their per-command entries.
        """
        data = _load_version(version)
        for flag in data.get("flags", []):
            fname = flag.get("flag_name")
            if fname in _EXCLUSIVE_PER_COMMAND_FLAGS and flag.get("command_name") is None:
                pytest.fail(
                    f"cli_flags_{version}.json: {fname!r} has command_name=null "
                    f"but should have a per-command command_name (deploy/scaffold/"
                    f"cloc/genproxytoken)"
                )


class TestV16V18DbSubcommands:
    """db subcommands (Gap 2) must be indexed in v16/v17/v18.

    Guards: 5 compound subcommand names appear in commands[]; db load has the
    correct per-version flags (--move from v17 on).
    """

    DB_VERSIONS = ["16.0", "17.0", "18.0"]

    # All 5 compound db subcommand names that must appear in commands[].
    DB_SUB_ACTIONS: frozenset[str] = frozenset({
        "db load", "db dump", "db duplicate", "db rename", "db drop",
    })

    # Flags required on db load in ALL of v16/v17/v18.
    DB_LOAD_REQUIRED: frozenset[str] = frozenset({
        "--dump-file", "--database", "--force", "--neutralize",
    })

    @pytest.mark.parametrize("version", DB_VERSIONS)
    def test_db_subcommands_in_commands_array(self, version: str) -> None:
        """All 5 db compound subcommands must appear in commands[] for v16/v17/v18."""
        data = _load_version(version)
        command_names = {c.get("name") for c in data.get("commands", [])}
        missing = self.DB_SUB_ACTIONS - command_names
        assert not missing, (
            f"cli_flags_{version}.json: missing db sub-actions in commands[]: "
            f"{sorted(missing)}"
        )

    @pytest.mark.parametrize("version", DB_VERSIONS)
    def test_db_load_required_flags_all_versions(self, version: str) -> None:
        """db load has --dump-file/--database/--force/--neutralize in all v16-18.

        These positionals must be normalized to --<name> per solution-338 §2.
        """
        data = _load_version(version)
        present = {
            f["flag_name"]
            for f in data.get("flags", [])
            if f.get("command_name") == "db load"
        }
        missing = self.DB_LOAD_REQUIRED - present
        assert not missing, (
            f"cli_flags_{version}.json: db load missing required flags: "
            f"{sorted(missing)}"
        )

    def test_db_load_move_exists_from_v17(self) -> None:
        """--move on db load exists from v17 on (the load sub-parser adds '--move' as a
        store_const flag), and not in v16, whose load sub-parser has no such option.
        The oracle test of ``TestCommandFlagsMatchTheRealSource`` checks every major
        against the real db.py; this pins the business rule where it is read first.
        """
        for version, present in (
            ("16.0", False), ("17.0", True), ("18.0", True), ("19.0", True), ("20.0", True),
        ):
            data = _load_version(version)
            db_load_flags = {
                f["flag_name"]
                for f in data.get("flags", [])
                if f.get("command_name") == "db load"
            }
            assert ("--move" in db_load_flags) is present, (
                f"cli_flags_{version}.json: db load --move should be "
                f"{'present' if present else 'absent'}"
            )


class TestGeoipAndGeventGap4:
    """Geoip primary name and gevent limit flags (Gap 4) must be correctly indexed.

    --geoip-city-db is the primary flag name in v17/v18 (--geoip-db became an
    alias/deprecated in those versions). Two gevent memory-limit flags are new
    in v18 only. Fails on base because these entries are absent or incorrectly
    named.
    """

    def test_geoip_city_db_primary_v17_v18(self) -> None:
        """v17 and v18 must index --geoip-city-db as a primary flag name.

        Current state: v17/v18 JSON only has --geoip-db and --geoip-country-db.
        Source confirms --geoip-city-db is the real primary in v17/v18 config.py;
        --geoip-db is a deprecated alias pointing to --geoip-city-db.
        v16 is excluded: --geoip-db IS the correct primary for v16.
        """
        for version in ("17.0", "18.0"):
            data = _load_version(version)
            flag_names = {f.get("flag_name") for f in data.get("flags", [])}
            assert "--geoip-city-db" in flag_names, (
                f"cli_flags_{version}.json: --geoip-city-db is missing; "
                f"it must be indexed as the primary geoip flag for {version}"
            )

    def test_geoip_city_db_absent_v16(self) -> None:
        """v16 must NOT have --geoip-city-db (--geoip-db is the correct primary for v16).

        This ensures the fix for v17/v18 does not incorrectly spill into v16.
        Source: v16 config.py only has --geoip-db.
        """
        data = _load_version("16.0")
        flag_names = {f.get("flag_name") for f in data.get("flags", [])}
        assert "--geoip-city-db" not in flag_names, (
            "cli_flags_16.0.json: --geoip-city-db must NOT be present "
            "(v16 uses --geoip-db as the primary; --geoip-city-db is v17+ only)"
        )

    # Business rule (source-verified against odoo{N}/.../tools/config.py):
    #   v8-v16: --geoip-db is the ONLY geoip database flag and is alive/primary
    #           -> it MUST be status="stable" with no replacement.
    #   v17+:   config.py renames it via add_option("--geoip-city-db", "--geoip-db", ...)
    #           so --geoip-db becomes a deprecated alias of --geoip-city-db
    #           -> it MUST be status="deprecated" with replacement="--geoip-city-db".
    _GEOIP_DB_STABLE_VERSIONS = [
        "8.0", "9.0", "10.0", "11.0", "12.0", "13.0", "14.0", "15.0", "16.0",
    ]
    _GEOIP_DB_DEPRECATED_VERSIONS = ["17.0", "18.0"]

    @pytest.mark.parametrize("version", _GEOIP_DB_STABLE_VERSIONS)
    def test_geoip_db_is_stable_primary_before_v17(self, version: str) -> None:
        """--geoip-db is a live primary flag (stable, no replacement) for v8-v16.

        Pre-fix bug: v8-v16 wrongly carried status="deprecated" +
        replacement_flag_name="--geoip-city-db", even though --geoip-city-db does
        not exist before v17. Source: odoo{N}/.../tools/config.py declares only
        add_option("--geoip-db", ...) in v8-v16.
        """
        data = _load_version(version)
        geoip_db = [
            f for f in data.get("flags", [])
            if f.get("flag_name") == "--geoip-db" and f.get("command_name") is None
        ]
        assert len(geoip_db) == 1, (
            f"cli_flags_{version}.json: expected exactly one global --geoip-db "
            f"entry, found {len(geoip_db)}"
        )
        entry = geoip_db[0]
        assert entry["status"] == "stable", (
            f"cli_flags_{version}.json: --geoip-db must be 'stable' (it is the "
            f"live primary geoip flag in v8-v16; --geoip-city-db only appears in "
            f"v17+), got {entry['status']!r}"
        )
        assert entry["replacement_flag_name"] is None, (
            f"cli_flags_{version}.json: --geoip-db must have no replacement before "
            f"v17, got {entry['replacement_flag_name']!r}"
        )

    @pytest.mark.parametrize("version", _GEOIP_DB_DEPRECATED_VERSIONS)
    def test_geoip_db_is_deprecated_alias_from_v17(self, version: str) -> None:
        """--geoip-db is a deprecated alias of --geoip-city-db from v17 onward.

        Source: v17/v18 config.py declares
        add_option("--geoip-city-db", "--geoip-db", ...) -> city-db is primary,
        db is the deprecated alias.
        """
        data = _load_version(version)
        geoip_db = [
            f for f in data.get("flags", [])
            if f.get("flag_name") == "--geoip-db" and f.get("command_name") is None
        ]
        assert len(geoip_db) == 1, (
            f"cli_flags_{version}.json: expected exactly one global --geoip-db "
            f"entry, found {len(geoip_db)}"
        )
        entry = geoip_db[0]
        assert entry["status"] == "deprecated", (
            f"cli_flags_{version}.json: --geoip-db must be 'deprecated' in {version} "
            f"(renamed to --geoip-city-db), got {entry['status']!r}"
        )
        assert entry["replacement_flag_name"] == "--geoip-city-db", (
            f"cli_flags_{version}.json: --geoip-db replacement must be "
            f"'--geoip-city-db' in {version}, got {entry['replacement_flag_name']!r}"
        )

    def test_gevent_limit_flags_v18(self) -> None:
        """v18 must index both gevent memory-limit flags (new in v18 config.py).

        Source: v18 config.py L346 (--limit-memory-soft-gevent) and
        L354 (--limit-memory-hard-gevent). Both have command_name=null
        (global flags) and type=int.
        """
        data = _load_version("18.0")
        flag_names = {f.get("flag_name") for f in data.get("flags", [])}
        for flag in ("--limit-memory-soft-gevent", "--limit-memory-hard-gevent"):
            assert flag in flag_names, (
                f"cli_flags_18.0.json: {flag!r} is missing; "
                f"this gevent memory-limit flag was added in v18 config.py"
            )


class TestCuratedDataStatesCurrentFacts:
    """The curated text states facts of the version, not where or when they were curated."""

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_flag_help_carries_no_source_location(self, version: str) -> None:
        # A "Source: file.py:NN" tail is a line number that rots with every upstream commit.
        located = [
            f"{f['command_name']} {f['flag_name']}"
            for f in _load_version(version)["flags"]
            if re.search(r"Source:|\b\w+\.py:\d+", f.get("help") or "")
        ]
        assert not located, f"cli_flags_{version}.json: help names a source location: {located}"

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_note_names_no_local_path_ticket_or_work_item(self, version: str) -> None:
        note = _load_version(version)["_note"]
        assert note.strip(), f"cli_flags_{version}.json: empty _note"
        assert not re.search(r"~/|/home/|#\d+|\bWI-|\bissue\b", note), (
            f"cli_flags_{version}.json: _note carries a local path or tracker reference: {note!r}"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_help_is_a_command_of_every_version(self, version: str) -> None:
        # ``odoo-bin help`` lists the commands in every series, 8.0 included.
        commands = {c["name"]: c for c in _load_version(version)["commands"]}
        assert "help" in commands, f"cli_flags_{version}.json: 'help' command missing"
        assert commands["help"]["description"] == "Display the list of available commands"

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_every_sub_action_has_its_parent_command(self, version: str) -> None:
        names = {c["name"] for c in _load_version(version)["commands"]}
        orphans = sorted(n for n in names if " " in n and n.split(" ", 1)[0] not in names)
        assert not orphans, f"cli_flags_{version}.json: sub-actions without parent: {orphans}"

    def test_the_config_file_variable_is_the_one_the_source_reads(self) -> None:
        # v8-v9 read OPENERP_SERVER only; v10+ read ODOO_RC (OPENERP_SERVER stays a fallback).
        for version in CURATED_VERSIONS:
            config = next(
                f for f in _load_version(version)["flags"]
                if f["flag_name"] == "--config" and f["command_name"] is None
            )
            expected = "OPENERP_SERVER" if version in ("8.0", "9.0") else "ODOO_RC"
            assert config["env_name"] == expected, f"cli_flags_{version}.json: --config"

    def test_libpq_variables_only_where_odoo_defers_to_libpq(self) -> None:
        # Before v19 Odoo passes host/port/user/password(/sslmode) to libpq only when the
        # flag is set; PGDATABASE, PGPATH and PGDATABASE_TEMPLATE are no variable it reads.
        for version in CURATED_VERSIONS[:CURATED_VERSIONS.index("19.0")]:
            env = {
                f["flag_name"]: f["env_name"] for f in _load_version(version)["flags"]
                if f["command_name"] is None and f["env_name"]
            }
            assert env.get("--db_host") == "PGHOST"
            assert not {"--database", "--pg_path", "--db-template"} & set(env), version

    def test_v19_posix_only_flags_are_exactly_the_posix_only_option_wrappers(self) -> None:
        posix = {
            f["flag_name"] for f in _load_version("19.0")["flags"] if f.get("posix_only")
        }
        assert posix == {
            "--workers", "--limit-memory-soft-gevent", "--limit-memory-hard",
            "--limit-memory-hard-gevent", "--limit-time-cpu", "--limit-request",
        }

    @pytest.mark.parametrize("version", ["19.0", "20.0"])
    def test_dev_flag_names_its_environment_variable(self, version: str) -> None:
        dev = next(f for f in _load_version(version)["flags"] if f["flag_name"] == "--dev")
        assert dev["env_name"] == "ODOO_DEV"

    def test_numeric_expression_defaults_are_the_numbers(self) -> None:
        for version in CURATED_VERSIONS:
            flags = {
                f["flag_name"]: f for f in _load_version(version)["flags"]
                if f["command_name"] is None
            }
            assert flags["--limit-memory-soft"]["default"] == "2147483648"
            assert flags["--limit-memory-hard"]["default"] == "2684354560"


# ---------------------------------------------------------------------------
# ADR-0055 oracles for the curated per-command data, every surveyed major.
#
# Per-command flags and sub-actions live in <pkg>/cli/*.py and addons/*/cli/*.py, which the
# production global-flag oracle (config.py) never opens, so these tests read the real
# declarations themselves. Era differences handled here:
#   * v8-v10 command files are Python 2 (print statements): ``ast.parse`` rejects the file, so
#     each ``add_option``/``add_argument`` call is isolated by the production text scanner
#     and parsed on its own;
#   * v16+ ``db`` (and v19+ ``i18n`` / ``module``) are argparse sub-parser trees: a flag is
#     scoped to the sub-parser variable it is declared on, a ``for p in (a, b, c)`` loop
#     scopes it to each member, and a flag every sub-action takes is indexed under the
#     parent command;
#   * v8/v9 live under ``openerp/``, v10+ under ``odoo/``.
# Facts compared per flag: the long name (a positional is normalised to ``--<name>``), the
# default when the declaration fixes it (a literal, a module constant, or the implicit
# false/true of a store action) and the type when the declaration fixes it. The ``help``
# texts of per-command flags are hand-written descriptions and have no mechanical oracle.
# ---------------------------------------------------------------------------

_OPTION_METHODS = ("add_argument", "add_option")
_BOOLEAN_ACTIONS = frozenset({"store_true", "store_false", "store_const"})
_UNDETERMINED = object()


def _terminal_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _declared_flag_name(call: ast.Call) -> str | None:
    strings = [
        a.value for a in call.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    long_names = [s for s in strings if s.startswith("--")]
    if long_names:
        return long_names[0]
    if strings and not strings[0].startswith("-"):
        return "--" + strings[0].replace("_", "-")
    return strings[0] if strings else None


def _declared_kwargs(call: ast.Call, constants: dict) -> dict:
    """Keyword values of an option call: the constant, or ``_UNDETERMINED`` when the value
    is not a constant of the file (a call, an attribute, an unknown name)."""
    out = {}
    for kw in call.keywords:
        if not kw.arg:
            continue
        value = _constant_value(kw.value)
        if value is _NOT_CONSTANT and isinstance(kw.value, ast.Name):
            value = constants.get(kw.value.id, _NOT_CONSTANT)
        out[kw.arg] = _UNDETERMINED if value is _NOT_CONSTANT else value
        if kw.arg == "type" and isinstance(kw.value, ast.Name):
            out["type"] = ("type", kw.value.id)
    return out


def _module_constants(tree: ast.Module) -> dict:
    constants = {}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            value = _constant_value(stmt.value)
            if value is not _NOT_CONSTANT:
                constants[stmt.targets[0].id] = value
    return constants


def _isolated_call(argument_list: str) -> ast.Call:
    """The option call whose argument list is *argument_list*, parsed on its own."""
    return parse_external_source("f(" + argument_list + ")").body[0].value


def declared_command_flags(source: str) -> dict[str, dict[str, dict]]:
    """{scope: {flag name: keyword values}} an odoo/cli file declares.

    Scope ``""`` is the command itself (or the options every sub-action shares); any other
    scope is the name of one sub-parser (``add_parser('init')``).
    """
    try:
        tree = parse_external_source(source)
    except SyntaxError:
        out: dict[str, dict[str, dict]] = {"": {}}
        for span in _find_option_call_arg_spans(source):
            call = _isolated_call(span)
            name = _declared_flag_name(call)
            if name:
                out[""].setdefault(name, _declared_kwargs(call, {}))
        return out

    constants = _module_constants(tree)
    sub_parsers: dict[str, str] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "add_parser" and node.value.args
                and isinstance(node.value.args[0], ast.Constant)):
            for target in node.targets:
                if _terminal_name(target):
                    sub_parsers[_terminal_name(target)] = node.value.args[0].value
    every_sub = set(sub_parsers.values())
    out = {"": {}, **{sub: {} for sub in every_sub}}

    def visit(node: ast.AST, loop_scopes: dict[str, set[str]]) -> None:
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            members = {
                sub_parsers[_terminal_name(e)] for e in node.iter.elts
                if _terminal_name(e) in sub_parsers
            }
            if members:
                loop_scopes = {**loop_scopes, node.target.id: members}
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _OPTION_METHODS):
            owner = _terminal_name(node.func.value)
            if owner in loop_scopes:
                scopes = loop_scopes[owner]
            elif owner in sub_parsers:
                scopes = {sub_parsers[owner]}
            else:
                scopes = {""}
            if every_sub and scopes >= every_sub:
                scopes = {""}
            name = _declared_flag_name(node)
            if name:
                for scope in scopes:
                    out[scope].setdefault(name, _declared_kwargs(node, constants))
        for child in ast.iter_child_nodes(node):
            visit(child, loop_scopes)

    visit(tree, {})
    return out


def _implied_default(kwargs: dict) -> object:
    """The default a declaration fixes, as the curated string; ``_UNDETERMINED`` when it
    depends on code (``default=template('default')``)."""
    action = kwargs.get("action")
    if action in ("store_true", "store_const"):
        return "false"
    if action == "store_false":
        return "true"
    default = kwargs.get("default", kwargs.get("my_default"))
    if default is _UNDETERMINED:
        return _UNDETERMINED
    return None if default is None else str(default)


def _implied_type(kwargs: dict) -> object:
    """The type a declaration fixes; ``_UNDETERMINED`` when the curated label is a free choice
    between equivalent spellings (str, path, ...)."""
    action = kwargs.get("action")
    if action in _BOOLEAN_ACTIONS:
        return None
    if action == "count":
        return "count"
    if "choices" in kwargs:
        return "choice"
    declared = kwargs.get("type")
    if declared in (("type", "int"), "int"):
        return "int"
    if declared in (("type", "float"), "float"):
        return "float"
    return _UNDETERMINED


def _curated_commands_by_file(data: dict) -> dict[str, list[str]]:
    by_file: dict[str, list[str]] = {}
    for command in data["commands"]:
        by_file.setdefault(command["file_path"], []).append(command["name"])
    return by_file


@pytest.mark.odoo_source
class TestCommandFlagsMatchTheRealSource:
    """Every curated per-command flag, sub-action and sub-action description is what the
    command file of the same major declares - no phantom, no missing flag."""

    @pytest.mark.parametrize("major", SURVEYED_MAJORS)
    def test_per_command_flags_match_the_declarations_in_the_real_command_files(
        self, major: int,
    ) -> None:
        root = checkout_root(major)
        if root is None:
            pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
        data = _load_version(f"{major}.0")
        problems: list[str] = []
        commands_with_files = set()
        for file_path, names in sorted(_curated_commands_by_file(data).items()):
            path = root / file_path
            assert path.is_file(), f"commands[] names {file_path} but it is not in the checkout"
            declared = declared_command_flags(path.read_text(encoding="utf-8", errors="ignore"))
            top_level = [n for n in names if " " not in n]
            assert len(top_level) == 1, f"{file_path}: expected one command, got {top_level}"
            for name in names:
                commands_with_files.add(name)
                scope = name.partition(" ")[2]
                if scope not in declared:
                    problems.append(f"{name}: no sub-parser {scope!r} in {file_path}")
                    continue
                curated = {f["flag_name"]: f for f in data["flags"] if f["command_name"] == name}
                source = declared[scope]
                if set(curated) != set(source):
                    problems.append(
                        f"{name}: curated-only {sorted(set(curated) - set(source))}, "
                        f"declared-only {sorted(set(source) - set(curated))}"
                    )
                for flag in sorted(set(curated) & set(source)):
                    for field_name, implied in (
                        ("default", _implied_default(source[flag])),
                        ("type", _implied_type(source[flag])),
                    ):
                        if implied is not _UNDETERMINED and curated[flag][field_name] != implied:
                            problems.append(
                                f"{name} {flag}: {field_name} {curated[flag][field_name]!r}, "
                                f"source implies {implied!r}"
                            )
        orphans = {
            f["command_name"] for f in data["flags"]
            if f["command_name"] not in (None, "server")
        } - commands_with_files
        if orphans:
            problems.append(f"flags under commands that are not in commands[]: {sorted(orphans)}")
        assert not problems, f"v{major}: " + "; ".join(problems)

    @pytest.mark.parametrize("major", SURVEYED_MAJORS)
    def test_sub_actions_are_the_sub_parsers_of_their_file_and_carry_their_help(
        self, major: int,
    ) -> None:
        root = checkout_root(major)
        if root is None:
            pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
        data = _load_version(f"{major}.0")
        descriptions = {c["name"]: c["description"] for c in data["commands"]}
        for file_path, names in sorted(_curated_commands_by_file(data).items()):
            tree = parse_external_source(
                (root / file_path).read_text(encoding="utf-8", errors="ignore"),
            ) if major >= 11 else None
            real: dict[str, str | None] = {}
            if tree is not None:
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "add_parser"):
                        help_kw = next((k.value for k in node.keywords if k.arg == "help"), None)
                        real[node.args[0].value] = (
                            help_kw.value if isinstance(help_kw, ast.Constant) else None
                        )
            top = next(n for n in names if " " not in n)
            curated = {n.partition(" ")[2] for n in names if " " in n}
            assert curated == set(real), (
                f"v{major} {file_path}: curated sub-actions {sorted(curated)}, "
                f"sub-parsers declared {sorted(real)}"
            )
            for sub, help_text in real.items():
                assert descriptions[f"{top} {sub}"] == help_text, (
                    f"v{major} {top} {sub}: description is not the sub-parser's help="
                )


# ---------------------------------------------------------------------------
# ADR-0055 oracle for the curated-only fields of the GLOBAL flags: ``env_name``,
# ``posix_only``, ``replacement_flag_name``. The sync oracle (cli_flags_sync) compares
# status / default / type / help only; these three fields come from other constructs, each
# read here from the real source of the same major:
#   env_name   v19+: the literal ``env_name=`` argument of the add_option call (the parser
#              reads it). Before v19 config.py has no such argument: the one environment
#              variable it reads is the one that locates the config file (``--config``), and
#              the flags that ``sql_db.connection_info_for`` leaves to libpq when empty
#              (its ``for p in ('host', 'port', ...)`` loop) are served by libpq's PG<NAME>
#              variable. Every other flag has no environment variable.
#   posix_only v19+: the option is wrapped in ``PosixOnlyOption(...)`` (the parser reads it);
#              before v19 it is declared inside an ``if os.name == 'posix':`` block.
#   replacement_flag_name: the successor of a flag that a later major (or the same major, for
#              a deprecated alias) declares; a command-level successor ("odoo-bin i18n
#              export ...") names a command that a later major ships. Whether a successor
#              EXISTS is mechanical; that a flag deserves one is a documented-successor
#              judgement, so a ``Deprecated alias to the X option`` help must name ``X``.
# ---------------------------------------------------------------------------

_POSIX_BLOCK = re.compile(r"^(?P<indent>[ \t]*)if os\.name == 'posix':[ \t]*$")
_CONFIG_FILE_ENV = re.compile(r"os\.environ\.get\('(\w+)'\)")
_LIBPQ_LOOP = re.compile(
    r"for p in \(([^)]*)\):\s*\n\s*cfg = tools\.config\['db_' \+ p\]"
)
_DEPRECATED_ALIAS_HELP = re.compile(r"Deprecated alias to the ([\w-]+) option")
_COMMAND_SUCCESSOR = re.compile(r"^odoo-bin (\w+(?: \w+)?)")


def _tools_file(root: Path, major: int, name: str) -> Path:
    package = "openerp" if _pkg_prefix(f"{major}.0") == "openerp" else "odoo"
    return root / package / name


def _real_global_flags(root: Path, major: int) -> dict:
    path = _tools_file(root, major, "tools/config.py")
    source = path.read_text(encoding="utf-8", errors="ignore")
    return {
        f.flag_name: f for f in _parse_options_calls(source, f"{major}.0", "server", str(path))
    }


def _flags_declared_in_posix_blocks(source: str) -> set[str]:
    lines = source.split("\n")
    found: set[str] = set()
    index = 0
    while index < len(lines):
        head = _POSIX_BLOCK.match(lines[index])
        if not head:
            index += 1
            continue
        indent = len(head.group("indent"))
        end = index + 1
        while end < len(lines) and (
            not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
        ):
            end += 1
        for span in _find_option_call_arg_spans("\n".join(lines[index + 1:end])):
            name = _declared_flag_name(_isolated_call(span))
            if name:
                found.add(name)
        index = end
    return found


def _source_env_name(root: Path, major: int, flag: str, real: dict) -> str | None:
    explicit = real[flag].env_name if flag in real else None
    if explicit or major >= 19:
        return explicit
    config_source = _tools_file(root, major, "tools/config.py").read_text(
        encoding="utf-8", errors="ignore")
    if flag == "--config":
        line = next(ln for ln in config_source.split("\n") if "opt.config or os.environ" in ln)
        return _CONFIG_FILE_ENV.findall(line)[0]
    sql_source = _tools_file(root, major, "sql_db.py").read_text(encoding="utf-8", errors="ignore")
    loop = _LIBPQ_LOOP.search(sql_source)
    assert loop, f"v{major}: sql_db.connection_info_for no longer has the libpq loop"
    libpq = {"--db_" + p.strip(" '\"") for p in loop.group(1).split(",")}
    return "PG" + flag[len("--db_"):].upper() if flag in libpq else None


@pytest.mark.odoo_source
class TestGlobalFlagCuratedOnlyFieldsMatchTheRealSource:
    @pytest.mark.parametrize("major", SURVEYED_MAJORS)
    def test_env_name_is_the_environment_variable_the_source_reads_for_the_flag(
        self, major: int,
    ) -> None:
        root = checkout_root(major)
        if root is None:
            pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
        real = _real_global_flags(root, major)
        wrong = []
        for flag in _load_version(f"{major}.0")["flags"]:
            if flag["command_name"] is not None:
                continue
            expected = _source_env_name(root, major, flag["flag_name"], real)
            if flag["env_name"] != expected:
                wrong.append(f"{flag['flag_name']}: {flag['env_name']!r}, source {expected!r}")
        assert not wrong, f"v{major} env_name: " + "; ".join(wrong)

    @pytest.mark.parametrize("major", SURVEYED_MAJORS)
    def test_posix_only_flags_are_the_options_the_source_restricts_to_posix(
        self, major: int,
    ) -> None:
        root = checkout_root(major)
        if root is None:
            pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
        real = _real_global_flags(root, major)
        blocks = _flags_declared_in_posix_blocks(
            _tools_file(root, major, "tools/config.py").read_text(encoding="utf-8", errors="ignore")
        )
        expected = {name for name, f in real.items() if f.posix_only} | blocks
        assert expected, f"v{major}: no POSIX-only option found - the oracle is blind"
        curated = {
            f["flag_name"] for f in _load_version(f"{major}.0")["flags"]
            if f["command_name"] is None and f["posix_only"]
        }
        assert curated == expected, (
            f"v{major} posix_only: curated-only {sorted(curated - expected)}, "
            f"source-only {sorted(expected - curated)}"
        )

    @pytest.mark.parametrize("major", SURVEYED_MAJORS)
    def test_replacement_is_a_flag_or_command_a_surveyed_major_declares(
        self, major: int,
    ) -> None:
        roots = {m: checkout_root(m) for m in SURVEYED_MAJORS if m >= major}
        if any(r is None for r in roots.values()):
            pytest.skip("a surveyed checkout is missing (set OSM_ODOO_CHECKOUTS)")
        later_flags = set()
        for m, root in roots.items():
            later_flags |= set(_real_global_flags(root, m))
            later_flags |= {
                f["flag_name"] for f in _load_version(f"{m}.0")["flags"] if f["command_name"]
            }
        later_commands = {
            c["name"] for m in roots for c in _load_version(f"{m}.0")["commands"]
        }
        wrong = []
        for flag in _load_version(f"{major}.0")["flags"]:
            replacement = flag["replacement_flag_name"]
            if replacement is None:
                continue
            command = _COMMAND_SUCCESSOR.match(replacement)
            known = (command.group(1) in later_commands) if command else (
                replacement in later_flags)
            if not known:
                wrong.append(f"{flag['flag_name']} -> {replacement!r}")
        assert not wrong, f"v{major}: successor no surveyed major declares: {wrong}"

    @pytest.mark.parametrize("major", SURVEYED_MAJORS)
    def test_a_deprecated_alias_help_names_the_replacement(self, major: int) -> None:
        for flag in _load_version(f"{major}.0")["flags"]:
            named = _DEPRECATED_ALIAS_HELP.search(flag["help"] or "")
            if named:
                assert flag["replacement_flag_name"] == f"--{named.group(1)}", (
                    f"v{major} {flag['flag_name']}: help says it is a deprecated alias of "
                    f"--{named.group(1)}, replacement is {flag['replacement_flag_name']!r}"
                )
