# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for curated cli_flags_<version>.json spec data files (WI-A5).

Validates:
- All 12 version files have _curate_status="complete" and len(flags) >= 20
- Every flag entry conforms to the cli_flag.schema.json schema
"""
import json
from pathlib import Path

import pytest

SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
SCHEMA_PATH = SPEC_DATA_DIR / "cli_flag.schema.json"

# All 12 versions that must be curated
CURATED_VERSIONS = [
    "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
]

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
    """All 12 cli_flags_*.json files must be marked complete with >= 20 flags."""

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
    correct per-version flags (--move only in v17). Fails on base because no
    db subcommand entries exist in v16-18 yet.
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

    def test_db_load_move_v17_only(self) -> None:
        """--move on db load exists ONLY in v17, not in v16 or v18.

        Source: v17 db.py adds '--move' (store_const, default=True, const=False)
        to the load subparser. This flag was not present in v16 and was removed
        before v18. Failing here means the version-specific flag was either
        omitted from v17 or incorrectly added to v16/v18.
        """
        # v17: --move must be present on db load
        data17 = _load_version("17.0")
        db_load_flags_v17 = {
            f["flag_name"]
            for f in data17.get("flags", [])
            if f.get("command_name") == "db load"
        }
        assert "--move" in db_load_flags_v17, (
            "cli_flags_17.0.json: db load is missing --move (v17-only flag)"
        )

        # v16 and v18: --move must NOT be present on db load
        for version in ("16.0", "18.0"):
            data = _load_version(version)
            db_load_flags = {
                f["flag_name"]
                for f in data.get("flags", [])
                if f.get("command_name") == "db load"
            }
            assert "--move" not in db_load_flags, (
                f"cli_flags_{version}.json: db load wrongly has --move "
                f"(this flag is v17-only)"
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
