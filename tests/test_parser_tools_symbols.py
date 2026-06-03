# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_tools_symbols.py
"""Unit tests for parser_tools_symbols._load_static_tools_symbols (ADR-0033).

Covers:
  1. Loader returns list[CoreSymbolInfo] with kind='tool_export' for all 12 versions.
  2. Per-version lifecycle correctness: SQL absent in 16.0, present in 17.0.
  3. safe_eval always uses qualified submodule path.
  4. image_resize_image present in v12, absent in v13+.
  5. format_datetime absent in v12, present in v13+.
  6. Returns empty list for a version without a JSON file.
  7. JSON schema validation for all 12 required versions.
"""
import json
from pathlib import Path

import pytest

from src.indexer.models import CoreSymbolInfo
from src.indexer.parser_tools_symbols import _load_static_tools_symbols

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
_SCHEMA_FILE = _SPEC_DATA_DIR / "tools_symbol.schema.json"

_REQUIRED_VERSIONS = [
    "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
]

_MIN_SYMBOLS_PER_VERSION = 10


# ---------------------------------------------------------------------------
# 1. Loader produces CoreSymbolInfo with kind='tool_export'
# ---------------------------------------------------------------------------

class TestLoaderReturnsCorrectObjects:
    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_loader_contract(self, version: str):
        """Loader contract for `_load_static_tools_symbols(version)` — all five
        per-version assertions merged into one parametrized check (fail message
        names the version + the contract that broke).

        Preserved assertions (one redundancy-free run per version):
          1. returns a list of >= _MIN_SYMBOLS_PER_VERSION items
          2. every item deserializes to a CoreSymbolInfo dataclass
          3. every item has kind == "tool_export"
          4. every item has odoo_version == <version> (version stamp)
          5. every qualified_name starts with "odoo.tools"
        """
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)

        # contract 1: non-empty list
        assert isinstance(symbols, list), f"Expected list for {version}"
        assert len(symbols) >= _MIN_SYMBOLS_PER_VERSION, (
            f"tools_symbols_{version}.json has only {len(symbols)} symbols; "
            f"expected >= {_MIN_SYMBOLS_PER_VERSION}"
        )

        for sym in symbols:
            # contract 2: deserialization guard
            assert isinstance(sym, CoreSymbolInfo), (
                f"Item in {version} is {type(sym)}, expected CoreSymbolInfo"
            )
            # contract 3: kind stamp
            assert sym.kind == "tool_export", (
                f"Symbol {sym.qualified_name} in {version} has kind={sym.kind!r}; "
                f"expected 'tool_export'"
            )
            # contract 4: version stamp
            assert sym.odoo_version == version, (
                f"Symbol {sym.qualified_name} has odoo_version={sym.odoo_version!r}; "
                f"expected {version!r}"
            )
            # contract 5: qname prefix
            assert sym.qualified_name.startswith("odoo.tools"), (
                f"Symbol {sym.qualified_name!r} in {version} does not start with 'odoo.tools'"
            )


# ---------------------------------------------------------------------------
# 2. odoo.tools.SQL lifecycle: absent v16, present v17
# ---------------------------------------------------------------------------

class TestSQLLifecycle:
    def test_sql_absent_in_v16(self):
        symbols = _load_static_tools_symbols("16.0", static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.SQL" not in qnames, (
            "odoo.tools.SQL must NOT be present in v16.0 (introduced v17)"
        )

    def test_sql_present_and_stable_in_v17(self):
        symbols = _load_static_tools_symbols("17.0", static_data_dir=_SPEC_DATA_DIR)
        sql_sym = next((s for s in symbols if s.qualified_name == "odoo.tools.SQL"), None)
        assert sql_sym is not None, "odoo.tools.SQL must be present in v17.0"
        assert sql_sym.status == "stable", (
            f"odoo.tools.SQL in v17.0 has status={sql_sym.status!r}; expected 'stable'"
        )

    def test_sql_present_in_v18_and_v19(self):
        for version in ("18.0", "19.0"):
            symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
            qnames = {s.qualified_name for s in symbols}
            assert "odoo.tools.SQL" in qnames, f"odoo.tools.SQL must be present in {version}"

    def test_sql_absent_in_v8_through_v15(self):
        for version in ("8.0", "9.0", "10.0", "11.0", "12.0", "13.0", "14.0", "15.0"):
            symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
            qnames = {s.qualified_name for s in symbols}
            assert "odoo.tools.SQL" not in qnames, (
                f"odoo.tools.SQL must NOT be present in {version}"
            )


# ---------------------------------------------------------------------------
# 3. safe_eval is NOT in curated tools_symbols (it is parsed from source)
# ---------------------------------------------------------------------------

class TestSafeEvalImportPath:
    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_safe_eval_absent_from_curated_symbols(self, version: str):
        """safe_eval must NOT appear in tools_symbols_*.json.

        odoo.tools.safe_eval.safe_eval is parsed directly from
        odoo/tools/safe_eval.py by parse_odoo_core() (kind='function').
        The curated JSON entry was removed (PR#160 FIX B) because:
          - Neo4j MERGE is last-write-wins on (qualified_name, odoo_version).
          - Appending tool_symbols AFTER parsed symbols caused the curated
            'tool_export' node to clobber the real parsed 'function' node.
          - Since parse_odoo_core already covers safe_eval, the curated entry
            is dead and can never survive the pipeline dedup filter.
        safe_eval lookup still works — the parsed node carries file_path +
        line number and is queried by _lookup_core_api via ENDS WITH.
        """
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        safe_eval_syms = [s for s in symbols if "safe_eval" in s.qualified_name]
        assert len(safe_eval_syms) == 0, (
            f"In {version}: found {len(safe_eval_syms)} curated safe_eval symbol(s) "
            f"{[s.qualified_name for s in safe_eval_syms]}; expected 0 — safe_eval is "
            f"parsed from source, not curated."
        )


# ---------------------------------------------------------------------------
# 4. image_resize_image: present v8-v12, absent v13+
# ---------------------------------------------------------------------------

class TestImageApiLifecycle:
    @pytest.mark.parametrize("version", ["8.0", "9.0", "10.0", "11.0", "12.0"])
    def test_image_resize_present_in_early_versions(self, version: str):
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.image_resize_image" in qnames, (
            f"odoo.tools.image_resize_image must be present in {version}"
        )

    @pytest.mark.parametrize("version", ["13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"])
    def test_image_resize_absent_from_v13(self, version: str):
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.image_resize_image" not in qnames, (
            f"odoo.tools.image_resize_image must NOT be present in {version} (removed v13)"
        )

    @pytest.mark.parametrize("version", ["13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"])
    def test_image_process_present_from_v13(self, version: str):
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.image_process" in qnames, (
            f"odoo.tools.image_process must be present in {version} (replacement since v13)"
        )


# ---------------------------------------------------------------------------
# 5. format_datetime: absent v8-v12, present v13+
# ---------------------------------------------------------------------------

class TestFormatDatetimeLifecycle:
    @pytest.mark.parametrize("version", ["8.0", "9.0", "10.0", "11.0", "12.0"])
    def test_format_datetime_absent_before_v13(self, version: str):
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.format_datetime" not in qnames, (
            f"odoo.tools.format_datetime must NOT be present in {version} (introduced v13)"
        )

    @pytest.mark.parametrize("version", ["13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"])
    def test_format_datetime_present_from_v13(self, version: str):
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.format_datetime" in qnames, (
            f"odoo.tools.format_datetime must be present in {version}"
        )


# ---------------------------------------------------------------------------
# 6. Returns empty list for unknown version
# ---------------------------------------------------------------------------

class TestMissingVersion:
    def test_returns_empty_list_for_unknown_version(self, tmp_path):
        # tmp_path has no tools_symbols_99.0.json
        result = _load_static_tools_symbols("99.0", static_data_dir=tmp_path)
        assert result == []

    def test_returns_empty_list_for_malformed_json(self, tmp_path):
        (tmp_path / "tools_symbols_99.0.json").write_text("{bad json", encoding="utf-8")
        result = _load_static_tools_symbols("99.0", static_data_dir=tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# 7. Schema validation for all 12 versions
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    @pytest.fixture(scope="class")
    def schema(self) -> dict:
        assert _SCHEMA_FILE.is_file(), f"Missing schema: {_SCHEMA_FILE}"
        return json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))

    def _validate_entry(self, entry: dict, schema: dict, version: str, idx: int) -> None:
        props = schema.get("properties", {})
        required = schema.get("required", [])

        for field in required:
            assert field in entry, (
                f"tools_symbols_{version}.json symbols[{idx}] missing required field '{field}'"
            )

        # qualified_name: non-empty string starting with odoo.tools
        qname = entry.get("qualified_name", "")
        assert isinstance(qname, str) and len(qname) >= 5, (
            f"tools_symbols_{version}.json symbols[{idx}].qualified_name invalid: {qname!r}"
        )
        assert qname.startswith("odoo.tools"), (
            f"tools_symbols_{version}.json symbols[{idx}].qualified_name={qname!r} "
            f"must start with 'odoo.tools'"
        )

        # kind: must be in enum
        kind_enum = props.get("kind", {}).get("enum", [])
        assert entry.get("kind") in kind_enum, (
            f"tools_symbols_{version}.json symbols[{idx}].kind={entry.get('kind')!r} "
            f"not in {kind_enum}"
        )

        # status: must be in enum
        status_enum = props.get("status", {}).get("enum", [])
        assert entry.get("status") in status_enum, (
            f"tools_symbols_{version}.json symbols[{idx}].status={entry.get('status')!r} "
            f"not in {status_enum}"
        )

        # No extra keys beyond schema properties
        allowed_keys = set(props.keys())
        extra_keys = set(entry.keys()) - allowed_keys
        if schema.get("additionalProperties") is False:
            assert not extra_keys, (
                f"tools_symbols_{version}.json symbols[{idx}] has unexpected keys: {extra_keys}"
            )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_all_entries_conform_to_schema(self, schema, version: str):
        path = _SPEC_DATA_DIR / f"tools_symbols_{version}.json"
        assert path.is_file(), f"Missing tools_symbols_{version}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])
        assert isinstance(symbols, list), f"tools_symbols_{version}.json 'symbols' must be a list"
        assert len(symbols) >= _MIN_SYMBOLS_PER_VERSION, (
            f"tools_symbols_{version}.json has only {len(symbols)} symbols; "
            f"expected >= {_MIN_SYMBOLS_PER_VERSION}"
        )
        for idx, entry in enumerate(symbols):
            assert isinstance(entry, dict), (
                f"tools_symbols_{version}.json symbols[{idx}] must be a dict"
            )
            self._validate_entry(entry, schema, version, idx)

    def test_schema_file_has_expected_structure(self, schema: dict):
        assert "$schema" in schema
        assert "properties" in schema
        assert "required" in schema
        required = schema["required"]
        assert "qualified_name" in required
        assert "kind" in required
        assert "status" in required

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_curate_status_complete(self, version: str):
        path = _SPEC_DATA_DIR / f"tools_symbols_{version}.json"
        assert path.is_file(), f"Missing tools_symbols_{version}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("_curate_status") == "complete", (
            f"tools_symbols_{version}.json has _curate_status={data.get('_curate_status')!r}; "
            f"expected 'complete'"
        )
