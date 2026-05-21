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
        data = _load_version(version)
        flags = data.get("flags", [])
        assert len(flags) >= MIN_FLAGS_PER_VERSION, (
            f"cli_flags_{version}.json: only {len(flags)} flags, "
            f"expected >= {MIN_FLAGS_PER_VERSION}"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_flags_key_is_list(self, version: str) -> None:
        data = _load_version(version)
        assert isinstance(data.get("flags"), list), (
            f"cli_flags_{version}.json: 'flags' must be a list"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_commands_key_present(self, version: str) -> None:
        data = _load_version(version)
        assert "commands" in data, (
            f"cli_flags_{version}.json: 'commands' key missing"
        )
        assert isinstance(data["commands"], list), (
            f"cli_flags_{version}.json: 'commands' must be a list"
        )

    @pytest.mark.parametrize("version", CURATED_VERSIONS)
    def test_server_command_always_present(self, version: str) -> None:
        """Every Odoo version must have a 'server' command."""
        data = _load_version(version)
        command_names = {c.get("name") for c in data.get("commands", [])}
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
        data = _load_version(version)
        flags = data.get("flags", [])
        names = [f.get("flag_name") for f in flags]
        seen: set[str] = set()
        dups = []
        for name in names:
            if name in seen:
                dups.append(name)
            seen.add(name)
        assert not dups, (
            f"cli_flags_{version}.json: duplicate flag_names: {dups}"
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
