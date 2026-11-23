# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_rules_v20_source_parity.py
"""Odoo 20.0 lint rule catalogue - behaviour and source-parity guards.

`src/indexer/spec_data/lint_rules_20.0.json` carries the 20.0 editorial layer
that the live sources (`_odoo_checker_*.py`, `eslintrc`, `ruff.toml`) cannot
express. Two layers guard it (ADR-0055: curated data needs an oracle wired to
a test on real data):

1. Behaviour (CI-safe, no checkout): every v20 rule whose detection is a
   `code_pattern` fires on a violating snippet and stays quiet on the v20
   replacement; rules whose premise is false at v20 are absent.
2. Source parity (`odoo_source`, skips per missing checkout): every
   version-boundary claim a v20 rule makes is diffed against the real 19.0 and
   20.0 checkouts - what the rule says is removed, replaced, deprecated or
   unsupported at v20 is defined at v19 and gone at v20.
"""
from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

from src.indexer.parser_lint_rules import _parse_pylint_odoo_source
from src.mcp.server import _match_lint_rule_lines
from tests._odoo_checkouts import checkout_root

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"


def _rules(version: str) -> dict[str, dict]:
    data = json.loads((_SPEC_DATA_DIR / f"lint_rules_{version}.json").read_text(encoding="utf-8"))
    by_id: dict[str, dict] = {}
    for rule in data["rules"]:
        assert rule["rule_id"] not in by_id, f"duplicate rule_id {rule['rule_id']} in {version}"
        by_id[rule["rule_id"]] = rule
    return by_id


_V20 = _rules("20.0")


def _fires(rule_id: str, code: str) -> bool:
    return bool(_match_lint_rule_lines(code, _V20[rule_id]))


# ---------------------------------------------------------------------------
# Layer 1 - behaviour of the v20 patterns (CI-safe, never skips).
# ---------------------------------------------------------------------------

# rule_id -> (snippets that violate the v20 rule, snippets that are the v20
# replacement or unrelated code and must not be flagged)
_PATTERN_CASES: dict[str, tuple[list[str], list[str]]] = {
    "W8192": (
        ['<tree string="Orders">', "<tree>", '<tree editable="bottom"/>'],
        ['<list string="Orders">', "<treeview>"],
    ),
    "W8210": (
        ["amount = fields.Float(group_operator='sum')"],
        ["amount = fields.Float(aggregator='sum')"],
    ),
    "W8211": (
        [
            "res = self.read_group(domain, ['amount'], ['partner_id'], lazy=False)",
            "res = self.read_group(domain, fields=['amount'], groupby=['partner_id'])",
            "res = self.read_group(domain, ['amount'], ['partner_id'], orderby='id')",
        ],
        ["res = self.read_group(domain, ['partner_id'], ['amount:sum'])"],
    ),
    "W8213": (
        ['QUnit.test("legacy", () => {});', "QUnit.module('x');"],
        ["// ported from the old test runner", 'test("hoot", () => {});'],
    ),
    "W8220": (
        ['@api.deprecated("Use something else")'],
        ['@deprecated("Use something else")'],
    ),
    "W8221": (
        [
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
            "'security/ir.model.access.csv',",
            "self.env['ir.model.access'].check('res.partner', 'read')",
        ],
        ["id,name,model_id,group_id/id,operation,domain", "'security/ir.access.csv',"],
    ),
    "W8222": (
        ['<record id="rule_x" model="ir.rule">', "self.env['ir.rule'].create(vals)"],
        ['<record id="rule_x" model="ir.access">', "res = self.env['res.rule']"],
    ),
    "W8223": (
        ["self.env.registry.clear_cache()", "registry.clear_cache('groups')"],
        ["self.env.transaction.invalidate_ormcache('groups')"],
    ),
    "W8224": (
        [
            "self.check_access_rights('read')",
            "rec.check_access_rule('write')",
            "recs = recs._filter_access_rules('read')",
        ],
        [
            "rec.check_access('read')",
            "ok = rec.has_access('write')",
            "recs._filtered_access('read')",
        ],
    ),
    "W8225": (
        ["if not self._check_recursion():", "self._check_m2m_recursion('child_ids')"],
        ["if self._has_cycle():", "self._has_cycle('child_ids')"],
    ),
    "W8226": (
        ["records.toggle_active()"],
        ["records.action_archive()", "    def test_toggle_active_warehouse_1(self):"],
    ),
    "W8227": (
        [
            "    _needaction = True",
            "    def view_init(self, fields_list):",
            "    _table_query = 'SELECT 1'",
            "    _sql = 'CREATE VIEW x'",
            "    def _execute_sql(self, query):",
        ],
        ["    _sql_constraints = []", "    def _table_sql(self):", "    def init(self):"],
    ),
    "W8228": (
        ["    _sql_constraints = [('name_uniq', 'unique(name)', 'Name must be unique.')]"],
        ["    _name_uniq = models.Constraint('unique(name)', \"Name must be unique.\")"],
    ),
    "W8229": (
        [
            "body = fields.Html(sanitize=False)",
            "body = fields.Html('Body', translate=True, sanitize=False)",
        ],
        ["body = fields.Html(sanitize=True)", "body = fields.Html('Body')"],
    ),
    "W8230": (
        ['<field name="datas" type="base64" file="base/static/img/x.png"/>'],
        ['<field name="datas" type="bytes" file="base/static/img/x.png"/>'],
    ),
    "W8231": (
        ['<i class="fa fa-user"/>', '<span t-attf-class="fa-lg {{ cls }}"/>', "<i class='fa'/>"],
        [
            '<i class="oi" data-icon="person"/>',
            '<div class="fancy text-fa o_fa"/>',
            '<div class="o_form_view"/>',
        ],
    ),
    "W8232": (
        [
            "from odoo.orm import models",
            "import odoo.orm.models",
            "from odoo import orm",
            "from odoo import api, orm",
        ],
        ["from odoo import api, fields, models", "from odoo.addons.base import x"],
    ),
    "W8233": (
        ["self._cr.execute(query)", "uid = self._uid", "ctx = self._context.get('lang')"],
        ["self.env.cr.execute(query)", "uid = self.env.uid", "self._context_extra = 1"],
    ),
    "W8234": (
        [
            '<filter name="d" date="date" start_month="-2" end_month="0"/>',
            "<filter start_year='-1'/>",
        ],
        ['<filter name="d" date="date"/>'],
    ),
    "owl/use-effect-single-callback": (
        ["useEffect(() => this.refresh(), () => [this.props.id]);"],
        [
            "useEffect(() => { this.refresh(); });",
            "useOnChange(() => [this.props.id], () => this.refresh());",
        ],
    ),
    "owl/use-effect-no-params": (
        ["useEffect((el) => { el.focus(); });", "useEffect(el => el.focus());"],
        ["useEffect(() => {", "useEffect(() => this.refresh());"],
    ),
    "owl/props-via-use-props": (
        ["    static props = { id: Number };"],
        ["    props = useProps({ id: t.number() });", "    static propsSchema = dialogProps;"],
    ),
}


@pytest.mark.parametrize("rule_id", sorted(_PATTERN_CASES))
def test_v20_pattern_flags_the_violation_and_spares_the_replacement(rule_id):
    """Business rule: a v20 rule must fire on the code it forbids and must not
    flag the v20 replacement - otherwise `lint_check` either misses the defect
    or tells the author to fix correct code."""
    assert _V20[rule_id].get("code_pattern"), f"{rule_id} must carry a deterministic code_pattern"
    violating, clean = _PATTERN_CASES[rule_id]
    for snippet in violating:
        assert _fires(rule_id, snippet), f"{rule_id} must flag: {snippet!r}"
    for snippet in clean:
        assert not _fires(rule_id, snippet), f"{rule_id} must not flag: {snippet!r}"


def test_every_v20_only_pattern_rule_has_behaviour_cases():
    """Business rule: a v20 rule that detects by code_pattern and was added or
    re-patterned for v20 is covered by `_PATTERN_CASES` (no pattern ships
    untested). Rules carried verbatim from v19 keep their v19 coverage."""
    v19 = _rules("19.0")
    changed = {
        rid for rid, rule in _V20.items()
        if rule.get("code_pattern")
        and (rid not in v19 or v19[rid].get("code_pattern") != rule["code_pattern"])
    }
    assert changed <= set(_PATTERN_CASES), (
        f"v20 pattern rules without behaviour cases: {sorted(changed - set(_PATTERN_CASES))}"
    )


@pytest.mark.parametrize("rule_id", ["W8177", "W8178", "W8204"])
def test_v20_drops_pylint_rules_whose_premise_is_false(rule_id):
    """Business rule: a rule whose premise does not hold at v20 is absent, not
    kept with a stale message (W8177 `_sql_constraints` messages, W8178 Html
    sanitize default, W8204 `_sql_constraints` tuples)."""
    assert rule_id not in _V20
    assert _rules("19.0")[rule_id]["kind"] == "pylint-odoo"


def test_v20_drops_ruff_e501_because_ruff_toml_ignores_it():
    """Business rule: a ruff rule listed in `ignore` is not an active v20 rule."""
    assert "E501" not in _V20
    assert _rules("19.0")["E501"]["kind"] == "ruff-builtin"


def test_v20_rules_keep_v19_provenance_for_shared_ids():
    """Business rule: provenance is a fact about the id - a rule id shared with
    v19 keeps its rule_id_source and its recorded collision."""
    v19 = _rules("19.0")
    for rid, rule in _V20.items():
        if rid in v19:
            assert rule["rule_id_source"] == v19[rid]["rule_id_source"], rid
            assert rule["rule_id_collision"] == v19[rid]["rule_id_collision"], rid
            assert rule["kind"] == v19[rid]["kind"], rid


def test_v20_note_names_the_source_commit():
    path = _SPEC_DATA_DIR / "lint_rules_20.0.json"
    note = json.loads(path.read_text(encoding="utf-8"))["_note"]
    assert "530bc2789e2f" in note


# ---------------------------------------------------------------------------
# Layer 2 - source parity against the real 19.0 / 20.0 checkouts.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root20() -> Path:
    root = checkout_root(20)
    if root is None:
        pytest.skip("Odoo 20 checkout not found (set OSM_ODOO_CHECKOUTS or ODOO20_SRC)")
    return root


@pytest.fixture(scope="module")
def root19() -> Path:
    root = checkout_root(19)
    if root is None:
        pytest.skip("Odoo 19 checkout not found (set OSM_ODOO_CHECKOUTS or ODOO19_SRC)")
    return root


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="ignore")


def _defining_files(root: Path, name: str) -> list[str]:
    """Python files under `odoo/` (framework, base addon included) defining `def <name>`."""
    pattern = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b", re.MULTILINE)
    return sorted(
        str(path.relative_to(root))
        for path in (root / "odoo").rglob("*.py")
        if "/tests/" not in str(path)
        and pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    )


def _find_function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"def {name} not found in {path}")


def _arg_names(fn: ast.FunctionDef) -> list[str]:
    a = fn.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


_REMOVED_AT_20 = {
    "W8224": ["check_access_rights", "check_access_rule", "_filter_access_rules"],
    "W8225": ["_check_recursion", "_check_m2m_recursion"],
    "W8226": ["toggle_active"],
}


@pytest.mark.odoo_source
@pytest.mark.parametrize(
    ("rule_id", "symbol"),
    [(rid, sym) for rid, syms in sorted(_REMOVED_AT_20.items()) for sym in syms],
)
def test_removed_in_v20_symbols_are_defined_at_v19_and_gone_at_v20(rule_id, symbol, root19, root20):
    """Business rule: "removed in v20" is true only if v19 still defines the
    symbol and v20 defines it nowhere in the framework."""
    assert "removed in v20" in _V20[rule_id]["message"]
    assert _defining_files(root19, symbol), f"{symbol} must still exist at v19"
    assert not _defining_files(root20, symbol), f"{symbol} must be gone at v20"


@pytest.mark.odoo_source
def test_w8223_registry_clear_cache_is_replaced_by_transaction_invalidate_ormcache(root19, root20):
    assert "removed in v20" in _V20["W8223"]["message"]
    assert "def clear_cache" in _read(root19, "odoo/orm/registry.py")
    assert "def clear_cache" not in _read(root20, "odoo/orm/registry.py")
    assert "def invalidate_ormcache" in _read(root20, "odoo/orm/environments.py")


@pytest.mark.odoo_source
def test_w8224_replacement_api_exists_at_v20(root20):
    models_py = _read(root20, "odoo/orm/models.py")
    for name in ("check_access", "has_access", "_filtered_access"):
        assert re.search(rf"def {name}\b", models_py), name
    assert re.search(r"def _has_cycle\b", models_py)
    assert re.search(r"def action_archive\b", models_py)
    assert re.search(r"def action_unarchive\b", models_py)


@pytest.mark.odoo_source
def test_w8220_api_deprecated_moved_to_tools_func(root19, root20):
    """`odoo.api` exports `deprecated` at v19 and not at v20, where
    `odoo.tools.func` is the home of `deprecated`."""
    assert re.search(r"^\s*deprecated,", _read(root19, "odoo/api/__init__.py"), re.MULTILINE)
    api20 = _read(root20, "odoo/api/__init__.py")
    assert not re.search(r"\bdeprecated\b", api20)
    assert "'deprecated'" in _read(root20, "odoo/tools/func.py")
    assert "removed in v20" in _V20["W8220"]["message"]


@pytest.mark.odoo_source
def test_w8107_w8108_api_multi_and_one_are_not_exported_at_v20(root20):
    api20 = _read(root20, "odoo/api/__init__.py")
    assert not re.search(r"\b(?:multi|one)\b", api20)


@pytest.mark.odoo_source
def test_w8211_read_group_signature_changed_at_v20(root19, root20):
    new = _arg_names(_find_function(root20 / "odoo/orm/models.py", "read_group"))
    old = _arg_names(_find_function(root19 / "odoo/orm/models.py", "read_group"))
    assert new == ["self", "domain", "groupby", "aggregates", "having", "offset", "limit", "order"]
    assert {"fields", "lazy", "orderby"} <= set(old)
    assert not {"fields", "lazy", "orderby"} & set(new)
    web20 = _read(root20, "addons/web/models/models.py")
    assert re.search(r"def formatted_read_group\b", web20)


@pytest.mark.odoo_source
def test_w8221_w8222_ir_access_replaces_model_access_and_rules(root19, root20):
    assert "_name = 'ir.access'" in _read(root20, "odoo/addons/base/models/ir_access.py")
    header = _read(root20, "odoo/addons/base/security/ir.access.csv").splitlines()[0]
    assert header == "id,name,model_id,group_id/id,operation,domain"
    assert not list((root20 / "odoo/addons").glob("*/security/ir.model.access.csv"))
    assert not list((root20 / "addons").glob("*/security/ir.model.access.csv"))
    assert not (root20 / "odoo/addons/base/models/ir_rule.py").exists()
    for models_py in (root20 / "odoo/addons/base/models").glob("*.py"):
        text = models_py.read_text(encoding="utf-8", errors="ignore")
        assert "_name = 'ir.model.access'" not in text
        assert "_name = 'ir.rule'" not in text
    # v19 still has the models and the csv this rule family replaces.
    assert (root19 / "odoo/addons/base/security/ir.model.access.csv").is_file()
    assert "_name = 'ir.rule'" in _read(root19, "odoo/addons/base/models/ir_rule.py")
    assert "_name = 'ir.model.access'" in _read(root19, "odoo/addons/base/models/ir_model.py")
    # No v20 data file declares an ir.rule record.
    xml_with_rule = [
        p for base in ("addons", "odoo/addons") for p in (root20 / base).glob("*/security/*.xml")
        if re.search(r"""model=["']ir\.rule["']""", p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not xml_with_rule, xml_with_rule[:3]


@pytest.mark.odoo_source
def test_w8228_sql_constraints_is_unsupported_and_constraint_replaces_it(root20):
    assert "'_sql_constraints' is no longer supported" in _read(root20, "odoo/orm/model_classes.py")
    table_objects = _read(root20, "odoo/orm/table_objects.py")
    assert re.search(r"^class Constraint\(", table_objects, re.MULTILINE)
    assert re.search(r"^class (?:Unique)?Index\(", table_objects, re.MULTILINE)
    offenders = [
        p for p in (root20 / "addons").glob("*/models/*.py")
        if re.search(
            r"^\s*_sql_constraints\s*=",
            p.read_text(encoding="utf-8", errors="ignore"),
            re.MULTILINE,
        )
    ]
    assert not offenders, offenders[:3]


@pytest.mark.odoo_source
def test_dropped_w8177_constraint_messages_are_plain_translated_strings(root20):
    """W8177 asked for `_()` around `_sql_constraints` messages. At v20 a
    `models.Constraint` message is stored in the translatable
    `ir.model.constraint.message` field, so core passes plain strings."""
    ir_model = _read(root20, "odoo/addons/base/models/ir_model.py")
    assert re.search(r"message = fields\.Char\([^)]*translate=True", ir_model)
    wrapped = [
        p for p in (root20 / "addons").glob("*/models/*.py")
        if re.search(
            r"models\.Constraint\([^)]*\b_\(",
            p.read_text(encoding="utf-8", errors="ignore"),
        )
    ]
    assert not wrapped, wrapped[:3]


@pytest.mark.odoo_source
def test_dropped_w8178_html_sanitizes_by_default(root20):
    textual = _read(root20, "odoo/orm/fields_textual.py")
    assert re.search(r"^\s*sanitize: bool = True\b", textual, re.MULTILINE)


@pytest.mark.odoo_source
def test_w8230_base64_xml_type_is_deprecated_at_v20(root20):
    convert_py = _read(root20, "odoo/tools/convert.py")
    assert "Since 20.0, use type=bytes instead of type=base64" in convert_py


@pytest.mark.odoo_source
def test_w8231_w8232_test_lint_enforces_icons_and_orm_imports(root20):
    icons = _read(root20, "odoo/addons/test_lint/tests/test_icons.py")
    assert "FA_CLASS_RE" in icons and "data-icon" in icons
    lint = _read(root20, "odoo/addons/test_lint/tests/test_python_lint.py")
    assert "def test_addons_orm_import" in lint


@pytest.mark.odoo_source
def test_w8233_model_private_properties_are_deprecated_in_v19_and_v20(root19, root20):
    for root in (root19, root20):
        models_py = _read(root, "odoo/orm/models.py")
        for attr in ("cr", "uid", "context"):
            message = f'("Deprecated since 19.0, use self.env.{attr} directly")'
            decorators = ("@api.deprecated", "@deprecated")
            assert any(f"{deco}{message}" in models_py for deco in decorators), attr


@pytest.mark.odoo_source
def test_w8227_deprecated_model_attributes_match_test_lint(root20):
    tree = ast.parse(_read(root20, "odoo/addons/test_lint/tests/test_model_attributes.py"))
    listed: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEPRECATED_MODEL_ATTRIBUTES" for t in node.targets
        ):
            listed = [elt.elts[0].value for elt in node.value.elts]
    expected = ["view_init", "_needaction", "_sql", "_table_query", "_execute_sql", "name_get"]
    assert sorted(listed) == sorted(expected)
    # name_get is covered by W8202; every other listed attribute is flagged as a definition.
    for attr in listed:
        if attr == "name_get":
            continue
        is_method = attr in ("view_init", "_execute_sql")
        snippet = f"    def {attr}(self):" if is_method else f"    {attr} = 1"
        assert _fires("W8227", snippet), attr
    assert re.search(r"def _table_sql\b", _read(root20, "odoo/orm/models.py"))
    assert re.search(r"def execute_query\b", _read(root20, "odoo/orm/environments.py"))


@pytest.mark.odoo_source
def test_w8234_search_date_filter_range_attributes_are_not_parsed_at_v20(root19, root20):
    parser19 = _read(root19, "addons/web/static/src/search/search_arch_parser.js")
    parser20 = _read(root20, "addons/web/static/src/search/search_arch_parser.js")
    assert "start_month" in parser19
    assert "start_month" not in parser20
    script = _read(root20, "odoo/upgrade_code/20.0-00-search-date-filters.py")
    assert "REMOVED_ATTRIBUTES = ('start_month', 'end_month', 'start_year', 'end_year')" in script


@pytest.mark.odoo_source
def test_w8192_tree_is_not_a_v20_view_type(root20):
    view_py = _read(root20, "odoo/addons/base/models/ir_ui_view.py")
    selection = re.search(r"type = fields\.Selection\(\[(.*?)\]", view_py, re.DOTALL).group(1)
    assert "('list'" in selection
    assert "'tree'" not in selection


@pytest.mark.odoo_source
def test_w8210_group_operator_is_not_translated_at_v20(root19, root20):
    assert "'group_operator' in attrs" in _read(root19, "odoo/orm/fields.py")
    assert "group_operator" not in _read(root20, "odoo/orm/fields.py")


@pytest.mark.odoo_source
def test_w8171_w8173_w8174_field_parameter_semantics_hold_at_v20(root20):
    fields_py = _read(root20, "odoo/orm/fields.py")
    assert "if 'depends' in attrs:" in fields_py
    relational = _read(root20, "odoo/orm/fields_relational.py")
    assert "self.ondelete = 'restrict' if self.required else 'set null'" in relational
    numeric = _read(root20, "odoo/orm/fields_numeric.py")
    assert ":param digits: a pair (total, decimal) or a string referencing" in numeric
    assert "deprecat" not in numeric.lower()


@pytest.mark.odoo_source
def test_w8101_to_w8104_manifest_key_contract_holds_at_v20(root20):
    module_py = _read(root20, "odoo/modules/module.py")
    assert "'version': '1.0'," in module_py
    assert "# - author\n    # - license\n    # - name" in module_py
    assert "defaulting to LGPL-3" in module_py


@pytest.mark.odoo_source
def test_v20_pylint_checkers_keep_the_v19_messages(root19, root20):
    """The three vendored pylint-odoo checkers declare the same rule ids and
    messages at v19 and v20, so the live layer and the curated E8501 entry
    carry over unchanged."""
    def msgs(root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted((root / "odoo/addons/test_lint/tests").glob("_odoo_checker_*.py")):
            for rule in _parse_pylint_odoo_source(path.read_text(encoding="utf-8"), "x", str(path)):
                out[rule.rule_id] = rule.message
        return out

    assert msgs(root20) == msgs(root19)
    assert {"E8501", "E8502", "E8503"} <= set(msgs(root20))


@pytest.mark.odoo_source
def test_v20_eslintrc_matches_the_owl3_rules_and_globals(root19, root20):
    eslint20 = _read(root20, "odoo/addons/test_lint/tests/eslintrc")
    eslint19 = _read(root19, "odoo/addons/test_lint/tests/eslintrc")
    assert "CallExpression[callee.name='useEffect'][arguments.length>1]" in eslint20
    assert (
        "CallExpression[callee.name='useEffect'] > :function:first-child[params.length>0]"
        in eslint20
    )
    assert "static='true']))" in eslint20  # the static template selector stays
    assert "Component should declare static props" in eslint19
    assert "Component should declare static props" not in eslint20
    assert '"QUnit": "readonly"' in eslint19 and "QUnit" not in eslint20
    assert '"SIP": "readonly"' in eslint20 and "SIP" not in eslint19


@pytest.mark.odoo_source
def test_owl3_facts_behind_the_v20_owl_rules(root19, root20):
    owl20 = _read(root20, "addons/web/static/lib/owl/owl.js")
    assert re.search(r'var version = "3\.', owl20)
    assert "useProps: () => useProps" in owl20 and "useOnChange: () => useOnChange" in owl20
    assert re.search(r'\bversion = "2\.', _read(root19, "addons/web/static/lib/owl/owl.js"))
    # Owl 3 components declare props with useProps; templates reach members through `this.`.
    assert "props = useProps(" in _read(root20, "addons/web/static/src/core/checkbox/checkbox.js")
    dialog_xml20 = _read(root20, "addons/web/static/src/core/dialog/dialog.xml")
    dialog_xml19 = _read(root19, "addons/web/static/src/core/dialog/dialog.xml")
    assert 't-if="this.props.header"' in dialog_xml20
    assert not re.search(r'(?<!this\.)\bprops\.header\b', dialog_xml20)
    assert re.search(r'(?<!this\.)\bprops\.', dialog_xml19)
    # Dialog keeps header/footer as named slots and the body as the default slot.
    assert 't-call-slot="default"' in dialog_xml20
    assert 't-call-slot="header"' in dialog_xml20 and 't-call-slot="footer"' in dialog_xml20
    assert 't-call-slot="body"' not in dialog_xml20


@pytest.mark.odoo_source
def test_w8213_qunit_is_not_used_by_v20_source(root20):
    hits = [
        p for p in (root20 / "addons").glob("*/static/src/**/*.js")
        if re.search(r"\bQUnit\s*\.", p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not hits, hits[:3]


@pytest.mark.odoo_source
def test_raw_contenteditable_rule_points_at_a_wysiwyg_that_exists_at_v20(root20):
    assert not (root20 / "addons/web_editor").exists()
    assert "export class Wysiwyg" in _read(root20, "addons/html_editor/static/src/wysiwyg.js")
    message = _V20["owl/raw-contenteditable"]["message"]
    assert "@html_editor/wysiwyg" in message and "web_editor" not in message


@pytest.mark.odoo_source
def test_v20_ruff_rules_are_selected_and_not_ignored_by_ruff_toml(root20):
    """Every curated ruff-builtin rule must be selected (directly or through its
    category) and not listed in ruff.toml's `ignore` at v20."""
    lint = tomllib.loads(_read(root20, "ruff.toml"))["lint"]
    selected, ignored = lint["select"], set(lint["ignore"])
    assert "E501" in ignored
    for rid, rule in _V20.items():
        if rule["kind"] != "ruff-builtin":
            continue
        assert rid not in ignored, f"{rid} is ignored by ruff.toml"
        assert any(rid == sel or rid.startswith(sel) for sel in selected), f"{rid} is not selected"
