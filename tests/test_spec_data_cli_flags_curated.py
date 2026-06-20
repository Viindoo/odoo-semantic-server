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
