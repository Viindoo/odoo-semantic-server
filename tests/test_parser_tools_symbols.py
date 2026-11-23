# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_tools_symbols.py
"""Unit tests for parser_tools_symbols._load_static_tools_symbols (ADR-0033).

Covers:
  1. Loader returns list[CoreSymbolInfo] with kind='tool_export' for every surveyed version.
  2. Per-version lifecycle correctness: SQL absent in 16.0, present in 17.0.
  3. safe_eval always uses qualified submodule path.
  4. image_resize_image present in v12, absent in v13+.
  5. format_datetime absent in v12, present in v13+.
  6. Returns empty list for a version without a JSON file.
  7. JSON schema validation for every surveyed version.
  8. Real jsonschema-library validation of every curated entry.
  9. note field loader-gap pin.
 10. v20 lifecycle: ustr removed, ormcache a deprecated alias, BinaryValue/BinaryBytes new.
 11. Oracle (parse_tools_symbols): packages and module-level __getattr__ on synthetic trees.
"""
import json
from pathlib import Path

import pytest

from src.constants import SURVEYED_MAJORS
from src.indexer.models import CoreSymbolInfo
from src.indexer.parser_tools_symbols import _load_static_tools_symbols, parse_tools_symbols

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
_SCHEMA_FILE = _SPEC_DATA_DIR / "tools_symbol.schema.json"

# Every surveyed version (SSOT: src.constants.SURVEYED_MAJORS).
_REQUIRED_VERSIONS = [f"{major}.0" for major in SURVEYED_MAJORS]


def _versions(min_major: int, max_major: int | None = None) -> list[str]:
    """Surveyed versions in [min_major, max_major] (open-ended when max is None)."""
    return [
        f"{major}.0" for major in SURVEYED_MAJORS
        if major >= min_major and (max_major is None or major <= max_major)
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

    def test_sql_present_from_v18(self):
        for version in _versions(18):
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

    @pytest.mark.parametrize("version", _versions(13))
    def test_image_resize_absent_from_v13(self, version: str):
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.image_resize_image" not in qnames, (
            f"odoo.tools.image_resize_image must NOT be present in {version} (removed v13)"
        )

    @pytest.mark.parametrize("version", _versions(13, 18))
    def test_image_process_present_from_v13(self, version: str):
        """19.0 and later deliberately excluded here - see
        test_image_process_flat_qname_removed_from_v19 below, not an oversight.
        Through v18 the flat `odoo.tools.image_process` re-export is the real,
        resolving import path, so checking its literal presence in the
        curated qnames correctly models "is image_process available here".
        """
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.image_process" in qnames, (
            f"odoo.tools.image_process must be present in {version} (replacement since v13)"
        )

    @pytest.mark.parametrize("version", _versions(19))
    def test_image_process_flat_qname_removed_from_v19(self, version: str):
        """v19 and later break the pattern test_image_process_present_from_v13 checks
        for v13-v18 (issue #364 Problem 2 - resolved here, not by weakening
        that test to also cover v19).

        Ground truth (verified against
        /home/tuan/git/odoo19/odoo/tools/__init__.py): the flat
        `from .image import image_process` re-export present through v18 was
        dropped at v19 - `odoo.tools.image_process` no longer resolves as an
        import path there (v19's own test_image.py imports the function via
        `from odoo.tools import image as tools` instead).

        Per tools_symbol.schema.json's own status doc ("'removed' = absent
        (do NOT include removed symbols - omit them instead)") - the same
        convention already applied to `odoo.tools.pycompat` inside this very
        v19.0 file - the curated data must OMIT the flat entry rather than
        keep it `deprecated`: `deprecated` means "available but discouraged"
        per the schema, and the flat name is not available at all at v19.
        Keeping it as `deprecated` + `replacement_qname` (as one might do for
        a symbol that still works, e.g. odoo.tools.html_escape ->
        markupsafe.escape) would misrepresent an unresolvable name as
        resolvable and reintroduce the exact two-convention inconsistency
        issue #364 was raised to close - the same situation (name gone at
        this version) must not be modelled two different ways in one data set.

        The capability itself is still present, just under its real
        resolving path - checked below.
        """
        symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in symbols}
        assert "odoo.tools.image_process" not in qnames, (
            f"odoo.tools.image_process (flat) must NOT be present in {version} - "
            "the re-export was dropped from odoo/tools/__init__.py at v19; "
            "keeping it (even as 'deprecated') misrepresents an unavailable "
            "name as available, contradicting the schema's own status "
            "semantics and the pycompat precedent in this same file"
        )

        replacement = next(
            (s for s in symbols if s.qualified_name == "odoo.tools.image.image_process"), None
        )
        assert replacement is not None and replacement.status == "stable", (
            f"odoo.tools.image.image_process must be present and 'stable' in "
            f"{version} - the real resolving path v19's own test_image.py imports "
            "('from odoo.tools import image as tools')"
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

    @pytest.mark.parametrize("version", _versions(13))
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
# 7. Schema validation for every surveyed version
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    @pytest.fixture(scope="class")
    @classmethod
    def schema(cls) -> dict:
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


# ---------------------------------------------------------------------------
# 8. Real jsonschema-library validation (issue #364 A7)
# ---------------------------------------------------------------------------
# TestSchemaValidation above is a manual, hand-rolled re-implementation of a
# subset of the schema's rules - it existed before this pass and is left in
# place unweakened. This class instead runs the ACTUAL `jsonschema` library
# (already a pinned dependency, pyproject.toml) against the real schema
# document, so every constraint the schema declares (including ones no one
# has hand-transcribed into a bespoke assertion, e.g. a property's declared
# `type`) is enforced, not just the subset a human remembered to re-check.
# Mirrors the pattern tests/test_patterns_schema.py already uses for
# patterns.schema.json.

class TestJsonschemaLibraryValidation:
    @pytest.fixture(scope="class")
    @classmethod
    def validator(cls):
        from jsonschema import validators
        schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
        validator_cls = validators.validator_for(schema)
        validator_cls.check_schema(schema)
        return validator_cls(schema)

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_all_entries_validate_against_real_jsonschema(self, validator, version: str):
        path = _SPEC_DATA_DIR / f"tools_symbols_{version}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = []
        for idx, entry in enumerate(data.get("symbols", [])):
            for err in validator.iter_errors(entry):
                errors.append(f"symbols[{idx}] ({entry.get('qualified_name')}): {err.message}")
        assert not errors, (
            f"tools_symbols_{version}.json has jsonschema violations:\n" + "\n".join(errors)
        )

    def test_validator_actually_rejects_a_broken_record(self, validator):
        """A validator that cannot fail is the thing issue #364 A7 eliminates -
        prove this one can, on a record shaped exactly like the 11 real
        pre-existing violations issue #364 A7 found in cli_flags (an int where
        the schema declares string)."""
        broken = {
            "qualified_name": "odoo.tools.example",
            "kind": "tool_export",
            "status": "stable",
            "signature": 42,  # schema requires ["string", "null"]
        }
        errors = list(validator.iter_errors(broken))
        assert errors, "expected the broken record (int signature) to fail validation"


# ---------------------------------------------------------------------------
# 9. note field - loader-gap pin (issue #364 C4)
# ---------------------------------------------------------------------------
# `note` is curated on every entry in every tools_symbols_<version>.json file
# and tools_symbol.schema.json documents it as
# "shown in tool output" - but `_load_static_tools_symbols` (this module)
# never reads the JSON `note` key, so it is discarded the instant the file is
# parsed and never reaches CoreSymbolInfo, Neo4j, or lookup_core_api. The
# write (writer_neo4j_spec.py) and render (src/mcp/tools/spec.py,
# tests/test_lookup_core_api_note_render.py) legs ARE wired and tested -
# only this loader's one-line gap remains, deliberately NOT closed in this
# pass (src/indexer/parser_tools_symbols.py is reserved for concurrent oracle
# work as of issue #364 S5 - see that file's git history/PR for the
# coordinating change).
#
# This class PINS today's known-gap behavior instead of leaving it latent.
# The moment someone adds `note=entry.get("note")` to the CoreSymbolInfo(...)
# call in `_load_static_tools_symbols`, this test legitimately starts
# failing - that failure is a SIGNAL the fix landed, not a regression to
# silence. Fix it by asserting notes ARE now populated (and delete this
# class's docstring caveat) as part of that same change - do not xfail or
# skip it in place (CLAUDE.md "no suppression without root-causing").

class TestNoteFieldPendingLoaderWiring:
    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_curated_notes_exist_but_loader_drops_them_today(self, version: str):
        path = _SPEC_DATA_DIR / f"tools_symbols_{version}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw_notes = [s.get("note") for s in raw.get("symbols", []) if s.get("note")]
        assert raw_notes, (
            f"tools_symbols_{version}.json: expected curated 'note' values on "
            f"disk (issue #364 C4 baseline) - none found; has the curated data "
            f"changed since this pin was written?"
        )

        loaded = _load_static_tools_symbols(version)
        assert loaded, f"loader returned nothing for {version}"
        loaded_notes = [s.note for s in loaded if s.note]
        assert loaded_notes == [], (
            f"tools_symbols_{version}.json: _load_static_tools_symbols now "
            f"populates CoreSymbolInfo.note ({len(loaded_notes)} non-null) - "
            f"the loader gap this pin tracks (issue #364 C4) has closed. "
            f"Update this test to assert notes ARE populated (e.g. compare "
            f"loaded_notes against raw_notes) and remove the stale docstring "
            f"caveat on TestNoteFieldPendingLoaderWiring; do not just delete "
            f"this assertion."
        )


# ---------------------------------------------------------------------------
# 10. v20 lifecycle (source-verified against odoo/odoo 20.0, see the _note of
#     tools_symbols_20.0.json; live-source proof lives in
#     tests/test_tools_symbols_content_parity.py)
# ---------------------------------------------------------------------------

def _curated(version: str) -> dict[str, CoreSymbolInfo]:
    return {
        s.qualified_name: s
        for s in _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
    }


class TestV20Lifecycle:
    @pytest.mark.parametrize("version", _versions(8, 19))
    def test_ustr_curated_through_v19(self, version: str):
        assert "odoo.tools.ustr" in _curated(version), (
            f"odoo.tools.ustr resolves at {version} and must stay curated there"
        )

    def test_ustr_removed_from_v20(self):
        """odoo/loglevels.py no longer defines ustr at 20.0, so the name does
        not resolve off odoo.tools; a name that does not resolve is omitted
        (tools_symbol.schema.json status doc), never kept as 'stable'."""
        assert "odoo.tools.ustr" not in _curated("20.0")

    def test_ormcache_is_a_deprecated_alias_of_odoo_api_ormcache_at_v20(self):
        entry = _curated("20.0").get("odoo.tools.ormcache")
        assert entry is not None, "odoo.tools.ormcache still resolves at 20.0 (deprecated alias)"
        assert entry.status == "deprecated"
        assert entry.replacement_qname == "odoo.api.ormcache"

    @pytest.mark.parametrize("name", ["BinaryValue", "BinaryBytes"])
    def test_binary_value_classes_are_stable_exports_at_v20(self, name: str):
        entry = _curated("20.0").get(f"odoo.tools.{name}")
        assert entry is not None and entry.status == "stable"
        assert f"odoo.tools.{name}" not in _curated("19.0"), (
            f"{name} is introduced at 20.0 (odoo/tools/binary.py)"
        )


# ---------------------------------------------------------------------------
# 11. Oracle on synthetic trees: first-party packages and module-level
#     __getattr__ (the real-checkout counterparts are in
#     tests/test_tools_symbols_content_parity.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def tools_tree(tmp_path):
    """A minimal odoo/tools tree shaped like v20: a package re-exporting its
    submodules, a payload package, a package shadowed by a served name, and a
    module-level __getattr__."""
    tools = tmp_path / "odoo" / "tools"
    (tools / "pkg").mkdir(parents=True)
    (tools / "pdf").mkdir()
    (tools / "served_pkg").mkdir()
    (tools / "__init__.py").write_text(
        "from .misc import *\n"
        "def __getattr__(name):\n"
        "    if name in ('served_pkg', 'served_alias'):\n"
        "        return 1\n"
        "    if name == 'served_single':\n"
        "        return 2\n"
        "    raise AttributeError(name)\n"
    )
    (tools / "misc.py").write_text("def helper():\n    pass\n")
    (tools / "pkg" / "__init__.py").write_text(
        "from .impl import *\nfrom .other import thing\n"
    )
    (tools / "pkg" / "impl.py").write_text(
        "__all__ = ['api_fn']\n"
        "def api_fn(a, /, b, *, c=1):\n    pass\n"
        "def hidden():\n    pass\n"
    )
    (tools / "pkg" / "other.py").write_text("def thing():\n    pass\n")
    (tools / "pdf" / "__init__.py").write_text("from .render import render_pdf\n")
    (tools / "pdf" / "render.py").write_text("def render_pdf():\n    pass\n")
    (tools / "served_pkg" / "__init__.py").write_text("def inner():\n    pass\n")
    return tmp_path


class TestOracleOnSyntheticTree:
    def test_package_surface_is_exposed_from_v20(self, tools_tree):
        parsed = parse_tools_symbols(tools_tree, "20.0")
        assert parsed is not None
        assert parsed["pkg"].signature == "module"
        assert parsed["pkg.api_fn"].file_path == "odoo/tools/pkg/impl.py"
        assert parsed["pkg.thing"].file_path == "odoo/tools/pkg/other.py"
        assert "pkg.hidden" not in parsed, "impl.py's __all__ gates the star re-export"

    def test_packages_are_not_modelled_before_v20(self, tools_tree):
        parsed = parse_tools_symbols(tools_tree, "19.0")
        assert parsed is not None
        assert "pkg" not in parsed
        assert not [k for k in parsed if k.startswith("pkg.")]

    def test_payload_packages_are_never_exposed(self, tools_tree):
        parsed = parse_tools_symbols(tools_tree, "20.0")
        assert parsed is not None
        assert "pdf" not in parsed
        assert "pdf.render_pdf" not in parsed

    def test_names_served_by_module_getattr_are_symbols(self, tools_tree):
        parsed = parse_tools_symbols(tools_tree, "20.0")
        assert parsed is not None
        for name in ("served_alias", "served_single"):
            assert parsed[name].qualified_name == f"odoo.tools.{name}"
            assert parsed[name].file_path == "odoo/tools/__init__.py"
        assert "__getattr__" not in parsed

    def test_served_name_wins_over_a_same_named_package(self, tools_tree):
        """Python consults the module __getattr__ before the submodule
        fallback, so the served name's origin is the tools __init__.py."""
        parsed = parse_tools_symbols(tools_tree, "20.0")
        assert parsed is not None
        assert parsed["served_pkg"].file_path == "odoo/tools/__init__.py"
