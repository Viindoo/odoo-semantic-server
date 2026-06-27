# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/models.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


def to_repo_relative(abs_path: str | None, repo_root: Path | str | None) -> str | None:
    """Return *abs_path* expressed relative to *repo_root* (portable form).

    Path portability (ADR-0037): stored/served paths must be relative to the
    repo root (e.g. ``addons/sale/models/sale_order.py``) so an AI client on a
    different machine can map them onto their own checkout — never the server's
    absolute filesystem path.

    Behaviour:
      * ``None``/empty in → returned unchanged.
      * ``repo_root is None`` → returned unchanged (caller had no anchor).
      * *abs_path* already relative (or simply not under *repo_root*) →
        ``relative_to`` raises ``ValueError`` → returned unchanged.  This makes
        the function idempotent: applying it to an already-relative path is a
        no-op, so it is safe to run on both legacy (absolute) and reindexed
        (relative) data.
    """
    if not abs_path:
        return abs_path
    if repo_root is None:
        return abs_path
    try:
        return str(Path(abs_path).relative_to(repo_root))
    except ValueError:
        return abs_path


@dataclass
class ModuleInfo:
    """Info for a single Odoo module.

    M4.6 WI1: `edition` ∈ {community/enterprise/viindoo/oca/custom},
    `viindoo_equivalent_qname` nullable string for EE-confusion lookup
    (e.g. user types `helpdesk` on Viindoo stack → suggest `viin_helpdesk`).
    A2b: manifest enrichment fields (auto_install, application, category,
    external_python, external_bin).
    A2c: repo provenance fields (repo_url, repo_id).
    """
    name: str
    odoo_version: str
    repo: str
    path: str
    depends: list[str]
    version_raw: str = ""
    edition: str = "community"
    viindoo_equivalent_qname: str | None = None
    commit_sha: str | None = None
    # ADR-0036 — License policy engine fields (always recorded, policy applied at registry).
    license: str | None = None
    copyright_owner: str | None = None
    license_notice: str | None = None
    # A2b — manifest enrichment
    auto_install: bool = False
    application: bool = False
    category: str | None = None
    summary: str | None = None
    external_python: list[str] = field(default_factory=list)
    external_bin: list[str] = field(default_factory=list)
    # v17+ manifest `countries` key: restricts module to these ISO country codes
    # for install-UI filtering (e.g. l10n_* modules). Empty = no restriction.
    countries: list[str] = field(default_factory=list)
    # A2c — repo provenance
    repo_url: str | None = None
    repo_id: int | None = None
    # ADR-0037 — transient absolute repo checkout root (NOT persisted to Neo4j).
    # Set by build_registry so writers can relativize file paths before storage.
    repo_root: Path | None = field(default=None, repr=False)

    def relative_path(self, abs_path: str | None) -> str | None:
        """Express *abs_path* relative to this module's repo root (ADR-0037).

        Idempotent: a path that is already relative (or not under repo_root)
        is returned unchanged.  Returns *abs_path* verbatim when repo_root is
        unset (e.g. ModuleInfo built outside build_registry, as in some tests).
        """
        return to_repo_relative(abs_path, self.repo_root)


@dataclass
class FieldInfo:
    """Info for a single Odoo field."""
    name: str
    ttype: str
    related: str | None = None
    compute: str | None = None
    stored: bool = True
    required: bool = False
    source_definition: str | None = None  # raw assignment line(s), for embedding
    comodel_name: str | None = None  # M10.5 P1 — comodel của Many2one/One2many/Many2many
    # A3 — provenance: 1-based source line in the .py file (era2 only; None for era1)
    line: int | None = None
    # A2-followup — field intent for AI agents: `string=` label + `help=` text.
    # era2: kwarg, else first positional arg for non-relational fields; era1 best-effort.
    string: str | None = None
    help: str | None = None
    # WI-1 (#238) — writability signals so AI clients don't set readonly/related
    # fields in create()/write() (ORM silently ignores → false green/red).
    # `readonly`: tri-state — explicit kwarg value (True/False) or None when absent.
    # `inverse`: name of the inverse method (str) or None — a related/compute field
    # with an inverse setter IS writable, so it must not be flagged readonly.
    # `effective_readonly`: derived (see parser_python._compute_effective_readonly);
    # the single signal renderers use to decide whether to flag a field readonly.
    # era1 (v8-9): best-effort — readonly/inverse left None, effective_readonly None
    # (no determination → renderer omits the readonly line entirely).
    readonly: bool | None = None
    inverse: str | None = None
    effective_readonly: bool | None = None


@dataclass
class MethodInfo:
    """Info for a method on an Odoo model.

    M4.6 WI2 — convention_kind / super_safety / return_required derived from
    the method name regex map (`_classify_method_convention` in parser_python).
    Used by `find_override_point` MCP tool to surface anti-patterns and
    super() guidance per ADR-0003 §3.
    A2a — docstring: captured via ast.get_docstring() in era2 AST extraction.
    A2d — field_refs: self.<x> direct attribute access names collected in era2.
    """
    name: str
    has_super_call: bool = False
    decorators: list[str] = field(default_factory=list)
    source_code: str | None = None  # raw method source, for embedding
    # M4.5 WI6 — qualified-name fragments (e.g. 'name_get', 'safe_eval') that
    # this method invokes. Used by writer_neo4j to MERGE USES_CORE_SYMBOL edges.
    # V0 scope: deprecated/removed symbols only — see parser_python._DEPRECATED_API_SYMBOLS.
    core_symbol_refs: list[str] = field(default_factory=list)
    # M4.6 WI2 — convention metadata (regex-derived, default = generic private).
    convention_kind: str = "private"
    super_safety: str = "usually"
    return_required: bool = False
    # M6 W3-7 — function argument signature string (e.g. "self, vals_list").
    # Captured via ast.unparse(node.args) in parser_python (era2 only; None for era1).
    signature: str | None = None
    # M10.5 P2 — @api.depends('field.subfield') dotted-path string args (era2 only;
    # [] for era1, which has no decorator depends). Used by validate_depends MCP tool.
    depends: list[str] = field(default_factory=list)
    # A2a — docstring extracted via ast.get_docstring() (era2 only; None for era1).
    docstring: str | None = None
    # A2d — direct self.<x> attribute access names (era2 only; [] for era1).
    # Only captures top-level self.x (NOT self.x.y chains — .y captured as x only).
    # Used by writer_neo4j to MERGE USES_FIELD / DEPENDS_ON_FIELD edges (best-effort).
    field_refs: list[str] = field(default_factory=list)
    # A3 — provenance: 1-based source line of the def statement (era2 only; None for era1)
    line: int | None = None


@dataclass
class ModelInfo:
    """Info for a single Odoo model."""
    name: str
    module: str
    odoo_version: str
    inherit: list[str] = field(default_factory=list)
    inherits: dict[str, str] = field(default_factory=dict)
    fields: list[FieldInfo] = field(default_factory=list)
    methods: list[MethodInfo] = field(default_factory=list)
    is_abstract: bool = False
    is_transient: bool = False
    had_explicit_name: bool = False  # True when _name = "..." appears in class body
    # A3 — provenance: absolute path of the .py file that defined this model
    # (set by parse_file after _parse_era2_ast / _parse_era1_text return)
    file_path: str | None = None


@dataclass
class ParseResult:
    """Parse result for a module: module info + list of models."""
    module: ModuleInfo
    models: list[ModelInfo] = field(default_factory=list)


@dataclass
class XPathInfo:
    """XPath modification entry in an extension view."""
    expr: str
    position: str  # before | after | inside | replace | attributes


@dataclass
class ViewConditionInfo:
    """One conditional-visibility expression extracted from a view's arch (GAP-1).

    Captures BOTH legacy (v8-v16) and modern (v17+) conditional-attribute forms
    in a single uniform shape so AI agents can answer "what makes this field
    invisible/required/readonly" at any version:

      * Legacy form (v8-v16): an element carries ``attrs="{'invisible': [...]}"``
        (a dict whose keys are ``invisible``/``required``/``readonly``/``column_invisible``
        and whose values are Odoo domains) and/or ``states="draft,sent"``. We emit
        one ViewConditionInfo per *attr* key (``attr='attrs.invisible'``,
        ``attr='states'``, ...), ``expr`` = the raw value string. ``legacy=True``.

      * Modern form (v17+): an element carries a direct expression attribute
        ``invisible="state == 'draft'"`` / ``required="..."`` / ``readonly="..."``
        / ``column_invisible="1"``. We emit one ViewConditionInfo per attribute,
        ``attr`` = the attribute name, ``expr`` = the expression value. ``legacy=False``.

    We deliberately do NOT evaluate the domain/expression - ``expr`` is the raw
    string. ``element`` is the local tag of the carrying node (e.g. ``field``,
    ``button``, ``page``); ``field`` is its ``name=`` when present (only meaningful
    for ``<field>``), else None.
    """
    element: str                 # local tag carrying the attribute, e.g. 'field'
    attr: str                    # 'invisible'|'required'|'readonly'|'column_invisible'
                                 # |'attrs.invisible'|...|'states'
    expr: str                    # raw expression / domain / states value
    field: str | None = None     # the field's name= when element == 'field', else None
    legacy: bool = False         # True for attrs=/states= (v8-v16); False for v17+ direct


@dataclass
class ViewInfo:
    """Info for a single Odoo ir.ui.view record."""
    xmlid: str           # "module.xml_id", e.g., "sale.view_sale_order_form"
    name: str
    model: str           # target Odoo model, e.g., "sale.order"
    module: str
    odoo_version: str
    view_type: str       # form | tree | list | kanban | search | pivot | graph | ...
    mode: str            # "primary" | "extension"
    inherit_xmlid: str | None
    xpaths: list[XPathInfo] = field(default_factory=list)
    arch: str | None = None       # serialized XML content of <arch> field, for embedding
    file_path: str | None = None  # source XML file path
    # A3 — provenance: 1-based source line of the <record> element
    # (best-effort from lxml .sourceline; None if unavailable)
    line: int | None = None
    # arch_snippet: first ~20-30 lines of arch (≤2000 chars) for base views only;
    # None for inherit-only (extension) views.  Stored on the Neo4j View node so AI
    # agents can inspect view structure without fetching the full arch body.
    arch_snippet: str | None = None
    # GAP-1 - conditional-visibility expressions extracted from arch (both the
    # legacy attrs=/states= form for v8-v16 AND the v17+ direct-expression form
    # invisible=/required=/readonly=/column_invisible=). Empty when the arch has
    # no conditional attributes. Walked over the FULL arch tree (not just the root)
    # so xpath-inserted fields in extension views are captured too.
    conditions: list[ViewConditionInfo] = field(default_factory=list)


@dataclass
class ReportInfo:
    """Info for a single Odoo report action (osm-audit-views GAP-2/GAP-5).

    Covers BOTH declaration forms, normalized to one shape:

      * v14+ ``<record model="ir.actions.report">`` — fields are <field> children:
        ``name`` (human label), ``model`` (the business model the report runs on),
        ``report_type`` (``qweb-pdf``/``qweb-html``/``qweb-text``),
        ``report_name`` (the template QWeb xmlid), ``report_file``, ``paperformat_id``.
      * v8-v13 ``<report .../>`` shorthand tag — attributes: ``id``, ``string``
        (human label -> name), ``model``, ``report_type``, ``name`` (the template
        QWeb xmlid -> report_name), ``file`` (-> report_file), optional
        ``paperformat`` / ``print_report_name``.

    ``report_name`` is the QWeb template xmlid the report renders (links to a
    :QWebTmpl node via USES_TEMPLATE). ``model`` is the business model the report
    targets (links to a :Model node via REPORTS_ON).

    Composite Neo4j key: (xmlid, odoo_version) — same shape as View/QWebTmpl.
    """
    xmlid: str                       # "module.report_action_id"
    name: str                        # human-readable report title
    model: str                       # business model, e.g. "sale.order"
    report_type: str                 # qweb-pdf | qweb-html | qweb-text | ...
    module: str
    odoo_version: str
    report_name: str | None = None   # template QWeb xmlid, e.g. "sale.report_saleorder"
    report_file: str | None = None   # template file ref (often == report_name)
    paperformat: str | None = None   # paperformat_id ref (xmlid), when present
    source_file: str | None = None   # source XML file path
    line: int | None = None          # 1-based source line (best-effort from lxml)


@dataclass
class QWebInfo:
    """Info for a single QWeb template."""
    xmlid: str           # "module.template_id"
    module: str
    odoo_version: str
    inherit_xmlid: str | None = None
    content: str | None = None    # serialized XML content of <template>, for embedding
    file_path: str | None = None  # source XML file path
    # A3 — provenance: 1-based source line of the <template> element
    # (best-effort from lxml .sourceline; None if unavailable)
    line: int | None = None
    # GAP-11 - website QWeb `key=` attribute (the canonical public xmlid that
    # multi-website page dispatch + extenders inherit by). None when absent (most
    # non-website templates). Captured from the <template key="..."> attribute.
    key: str | None = None
    # GAP-12 - `mode=` attribute on an inheriting template: "primary" creates a
    # NEW primary view from the inherited one (a fresh copy), "extension" (default)
    # patches in place. None when absent.
    mode: str | None = None


@dataclass
class AssetBundleContribution:
    """A single Module -> AssetBundle contribution (WI-D, ADR-0052).

    Captures one bundle that *module* contributes entries to, plus the ordered
    list of entries it contributes. An *entry* is the manifest grammar form,
    normalized to JSON-serializable shapes:
      - str: a file path or glob (leading '/' stripped — ADR-0037-style portable).
      - list[str]: a tuple operation, e.g. ['include', 'web._assets_helpers'],
        ['remove', 'web/.../foo.js'], ['replace', ref, new], ['before', ref, new],
        ['after', ref, new], ['prepend', path].
    `includes` is the subset of bundle names referenced via ('include', name) —
    used to write INCLUDES_BUNDLE edges (AssetBundle -> AssetBundle).
    """
    module: str
    odoo_version: str
    bundle_name: str          # full dotted name, e.g. 'web.assets_backend'
    entries: list = field(default_factory=list)        # ordered str | list[str]
    includes: list[str] = field(default_factory=list)  # ('include', X) targets


@dataclass
class AssetParseResult:
    """Output of parser_assets.parse_assets() for one module (WI-D, ADR-0052).

    Era B (v15+): `contributions` = the module's manifest `'assets'` dict, one
    AssetBundleContribution per bundle. Era A (v8-14): empty — legacy XML
    `<template>` bundle definitions/extensions are already captured by
    parser_qweb (definitions -> QWebTmpl base nodes; extenders -> QWebInfo with
    inherit_xmlid), so the era-A handler emits no separate contributions and the
    writer resolves legacy extenders against either a QWebTmpl OR an AssetBundle.
    """
    module: "ModuleInfo"
    contributions: list[AssetBundleContribution] = field(default_factory=list)


@dataclass
class JSChunk:
    """A chunk of JavaScript code from a module, for embedding."""
    module: str
    odoo_version: str
    file_path: str
    era: str            # 'era1' | 'era2' | 'era3'
    entity_name: str    # widget/component name, or file stem if unknown
    chunk_idx: int
    content: str        # raw JS snippet (~512 tokens)


@dataclass
class ViewParseResult:
    """Parse result for XML files in a module: views + qweb templates.

    `lint_violations` contains RelaxNG validation errors (v15+ only) collected
    by `parser_xml` using the version-exact RNG read from the indexed Odoo core
    source tree at index time (no vendored copy).
    """
    module: ModuleInfo
    views: list[ViewInfo] = field(default_factory=list)
    qweb: list[QWebInfo] = field(default_factory=list)
    # GAP-2/GAP-5: ir.actions.report records + v8-v13 <report> shorthand.
    # Placed AFTER qweb so existing positional ViewParseResult(...) constructors
    # in tests stay valid.
    reports: list["ReportInfo"] = field(default_factory=list)
    lint_violations: list["LintViolationInfo"] = field(default_factory=list)


@dataclass
class JSPatchInfo:
    """A JS patch on an OWL component or legacy widget."""
    target: str             # patched component/widget name
    patch_name: str         # patch identifier (or file stem)
    module: str
    odoo_version: str
    era: str                # 'extend' (era1) | 'include' (era2) | 'patch' (era3)
    file_path: str


@dataclass
class OWLCompInfo:
    """An OWL component class declaration."""
    name: str
    module: str
    odoo_version: str
    template: str | None = None    # `static template = "..."` if found
    extends: str | None = None     # superclass name if extends Component
    bound_model: str | None = None  # heuristic from props/services usage
    file_path: str = ""


@dataclass
class JSGraphResult:
    """JS graph extraction result for a module."""
    module: ModuleInfo
    patches: list[JSPatchInfo] = field(default_factory=list)
    components: list[OWLCompInfo] = field(default_factory=list)


# --- CSS/SCSS layer (WI-A1, per ADR-0025) -----------------------------------


@dataclass
class CSSChunk:
    """A semantic chunk from a CSS file — variable block, selector group, or @media query."""
    module: str
    odoo_version: str
    file_path: str
    chunk_kind: str     # 'variable' | 'selector' | 'media' | 'import' | 'raw'
    entity_name: str    # e.g. selector text, variable group prefix, or file stem
    chunk_idx: int
    content: str        # raw CSS snippet (~2048 chars)


@dataclass
class SCSSChunk:
    """A semantic chunk from a SCSS file — mixin, variable block, selector, or @extend."""
    module: str
    odoo_version: str
    file_path: str
    chunk_kind: str     # 'mixin' | 'variable' | 'selector' | 'extend' | 'media' | 'import' | 'raw'
    entity_name: str    # mixin name, selector text, variable group prefix, or file stem
    chunk_idx: int
    content: str        # raw SCSS snippet (~2048 chars)


@dataclass
class StylesheetInfo:
    """Metadata summary for a CSS, SCSS, or LESS file in an Odoo module.

    Composite Neo4j key: (file_path, module, odoo_version).
    Written as :Stylesheet node with :DEFINED_IN -> :Module edge.
    :Stylesheet -[:IMPORTS]-> :Stylesheet edges represent @import chains.
    """
    file_path: str          # absolute path to the .css, .scss, or .less file
    module: str             # Odoo module name
    odoo_version: str
    language: str           # 'css' | 'scss' | 'less'
    selector_count: int = 0
    variable_count: int = 0
    import_count: int = 0
    mixin_count: int = 0    # SCSS/LESS mixins; always 0 for plain CSS
    imports: list[str] = field(default_factory=list)  # resolved file_paths of @import targets


# --- Pattern layer (M4.6, per ADR-0003) -------------------------------------


@dataclass
class PatternExample:
    """A curated Odoo idiom snippet — pattern_id keyed, language-tagged.

    Lives in Neo4j as `(:PatternExample)` (composite key: `pattern_id`).
    The `snippet_text` + `gotchas` are also embedded as a `pattern_example`
    chunk in the `embeddings` table for `suggest_pattern` ANN search per
    ADR-0003 §1. `core_symbol_names` MERGE USES_CORE_SYMBOL edges with
    silent skip when the target CoreSymbol does not exist (M4.5 graceful).
    """
    pattern_id: str
    intent_keywords: list[str]
    file_ref: str            # 'addons/sale/models/sale_order.py:245'
    snippet_text: str        # 3-5 line canonical excerpt
    gotchas: list[str]
    odoo_version_min: str
    language: str            # 'python' | 'xml' | 'js'
    core_symbol_names: list[str] = field(default_factory=list)
    # #329 - upper bound of the [min, max] version window this pattern applies to.
    # None = open-ended (valid for every version >= odoo_version_min). Placed AFTER
    # core_symbol_names so existing positional constructors stay valid. suggest_pattern
    # filters out patterns whose range excludes the resolved query version (era1/era2
    # split: e.g. SavepointCase is v8-v15, must NOT surface for a v17 query).
    odoo_version_max: str | None = None
    # #331 - optional bucket for the pattern. Values: 'test' | 'production' | None (uncategorized).
    # Placed AFTER odoo_version_max so existing positional constructors stay valid.
    # suggest_pattern can filter by category (e.g. category='test' surfaces only
    # test-writing idioms; category='production' surfaces only production idioms).
    category: str | None = None


# --- Spec layer (M4.5, per ADR-0002) ----------------------------------------

@dataclass
class CoreSymbolInfo:
    """An Odoo upstream API entity, captured per version.

    Composite key: (qualified_name, odoo_version) — see ADR-0002 §1.
    `kind` ∈ {function, class, decorator, exception, field_type, orm_method, cursor_method,
              tool_export}.
    `status` ∈ {stable, deprecated, removed, added}.
    `replacement_qname` non-null when this symbol is superseded by another.
    """
    qualified_name: str
    kind: str
    odoo_version: str
    signature: str | None = None
    file_path: str | None = None
    line: int | None = None
    status: str = "stable"
    replacement_qname: str | None = None


@dataclass
class LintRuleInfo:
    """A lint rule (pylint-odoo / ESLint / ruff) captured per Odoo version.

    Composite key: (rule_id, odoo_version) — see ADR-0002 §1.
    `kind` ∈ {pylint-odoo, pylint-stdlib, eslint-odoo, ruff-builtin}.
    `severity` ∈ {error, warning, info}.
    `core_symbol_qname` links the rule to a CoreSymbol when the rule checks
    one specific API (e.g. unlink-override → odoo.models.BaseModel.unlink).
    """
    rule_id: str
    odoo_version: str
    kind: str
    message: str | None = None
    severity: str = "warning"
    file_pattern: str | None = None
    fix_template: str | None = None
    core_symbol_qname: str | None = None
    code_pattern: str | None = None


@dataclass
class CLICommandInfo:
    """An odoo-bin subcommand (e.g. server, shell, scaffold, db).

    Composite key: (name, odoo_version) — see ADR-0002 §1.
    """
    name: str
    odoo_version: str
    description: str | None = None
    file_path: str | None = None


@dataclass
class CLIFlagInfo:
    """A CLI flag belonging to a subcommand (e.g. --http-port on server).

    Composite key: (flag_name, command_name, odoo_version) — see ADR-0002 §1.
    `status` ∈ {stable, deprecated, removed, added}.
    `replacement_flag_name` points to the successor flag when this one is
    deprecated/removed (e.g. --longpolling-port → --gevent-port).
    """
    flag_name: str
    command_name: str
    odoo_version: str
    status: str = "stable"
    default: str | None = None
    type: str | None = None
    help: str | None = None
    replacement_flag_name: str | None = None
    env_name: str | None = None
    posix_only: bool = False


# --- RelaxNG XML lint violation layer (WI-E, M11) ----------------------------


@dataclass
class LintViolationInfo:
    """A RelaxNG validation error found in a View's arch XML (WI-E, M11).

    Collected during XML parsing (v15+ only, via VersionRegistry gate) and
    written as :LintViolation Neo4j nodes tied to the owning :View node.

    Composite MERGE key: (file_path, line, rule, odoo_version).

    Fields:
        file_path:    Absolute path of the source XML file.
        line:         1-based line number within the arch element where the
                      error was reported (0 when unavailable).
        rule:         Short rule identifier, e.g. 'relaxng.tree_view'.
        message:      Raw error message from the RelaxNG validator.
        view_xmlid:   Full xmlid of the owning view, e.g. 'sale.view_order_tree'.
        odoo_version: Odoo version label, e.g. '17.0'.
        severity:     Always 'error' for RelaxNG violations (schema mismatch).
        view_type:    View type that was validated, e.g. 'tree', 'search'.
    """
    file_path: str
    line: int
    rule: str
    message: str
    view_xmlid: str
    odoo_version: str
    severity: str = "error"
    view_type: str = ""


# --- Test surface index layer (WI-1) -----------------------------------------


@dataclass
class TestMethodInfo:
    """A single test_* method (or setUp/setUpClass) extracted from a test class.

    Composite MERGE key: (name, test_class, module, file_path, odoo_version) -
    CRITICAL-1: file_path in key because same test class name may appear in
    multiple files within the same module (e.g. sale.tests.common + sale.tests.test_common).

    `via` on each field_ref tracks whether the ref came from 'setup', 'assert', or 'body'.
    `asserts_count` is the number of self.assert*/assertEqual/assertIn/assertFalse etc. calls.
    """
    # pytest must NOT collect this dataclass as a test class (M5: its name starts
    # with 'Test' + it has an __init__, triggering PytestCollectionWarning). The
    # idiomatic opt-out is __test__ = False, fixing the warning at source (CLAUDE.md
    # forbids filterwarnings suppression). ClassVar so it is not a dataclass field.
    __test__: ClassVar[bool] = False

    name: str
    test_class: str               # owning class name (for MERGE key)
    module: str
    file_path: str                # repo-relative (ADR-0037)
    odoo_version: str
    tagged: list[str] = field(default_factory=list)
    docstring: str | None = None
    field_refs: list[str] = field(default_factory=list)
    model_refs: list[str] = field(default_factory=list)
    method_refs: list[str] = field(default_factory=list)
    asserts_count: int = 0
    via: str = "body"            # 'setup' | 'assert' | 'body' — for COVERS_* edge property
    line: int | None = None
    source_code: str | None = None


@dataclass
class TestClassInfo:
    """A Python class inside a test file, regardless of base class.

    EVERY ClassDef in a test file emits a node (HIGH-1): non-Case mixins
    (MailCase, MockEmail, TestSaleCommonBase) get nodes + INHERITS_TEST edges too.

    Composite MERGE key: (name, module, file_path, odoo_version) - CRITICAL-1.
    `file_path` is in the key because two files in the same module may define
    a class with the same name (proven: sale v17 has two TestSaleCommon).

    `base_classes_ordered` preserves Python MRO declaration order (HIGH-1).
    `TEST_BASE_CLASSES` classifies test_type; it never gates emission.
    `defines_no_test_methods` is provisional; `is_helper` is finalized in the
    reconcile pass after cross-file base resolution is complete (MISSED-1).
    """
    __test__: ClassVar[bool] = False  # M5: pytest must not collect this dataclass

    name: str
    module: str
    file_path: str                # repo-relative (ADR-0037), part of MERGE key
    odoo_version: str
    # test_type ∈ 'transaction'|'savepoint'|'single_transaction'|'http'|'form'|'unittest'|'unknown'
    test_type: str = "unknown"
    base_classes_ordered: list[str] = field(default_factory=list)  # MRO order (HIGH-1)
    tagged: list[str] = field(default_factory=list)  # raw incl '-tag' entries (MISSED)
    commit_allowed: bool = False  # True only for @standalone (PP3)
    defines_no_test_methods: bool = False  # provisional; is_helper finalized in reconcile
    is_helper: bool = False
    docstring: str | None = None
    line: int | None = None
    methods: list[TestMethodInfo] = field(default_factory=list)


@dataclass
class TestHelperInfo:
    """A known reusable base class for tests.

    Covers two origins:
    - 'framework': built-in Odoo test bases (TransactionCase, HttpCase, etc.)
      seeded from odoo/tests/common.py by parser_odoo_core. These get NO
      DEFINED_IN edge and use module='@framework' (MED-3: avoids confusion with
      the '__unresolved__' GC placeholder).
    - 'addon': a TestClass promoted to TestHelper after reconcile (is_helper=True
      on the TestClass + defines no test_* methods but is subclassed).

    Composite MERGE key: (name, module, odoo_version).
    """
    __test__: ClassVar[bool] = False  # M5: pytest must not collect this dataclass

    name: str
    module: str                  # '@framework' for framework bases (MED-3)
    odoo_version: str
    origin: str = "addon"        # 'addon' | 'framework'
    test_type: str = "unknown"
    setup_summary: list[str] = field(default_factory=list)  # model names created in setUpClass
    commit_allowed: bool = False
    file_path: str | None = None
    line: int | None = None


@dataclass
class JsTestSuiteInfo:
    """A JavaScript test file (Hoot/QUnit/tour) — file-grained (not per-test).

    `mock_models` captures defineModels()/_name assignments from hand-rolled
    test-double models (MED-1: these are NOT real Odoo models; no COVERS_MODEL
    edge is emitted for them). `mounts` captures resModel from mountView() calls.

    Composite MERGE key: (file_path, module, odoo_version).
    JS parsing is WI-3; this dataclass is present here so TestParseResult can
    carry a js_suites list that WI-3 populates. WI-1 leaves extraction unimplemented.
    """
    file_path: str               # repo-relative (ADR-0037), part of MERGE key
    module: str
    odoo_version: str
    framework: str = "unknown"  # 'hoot' | 'qunit' | 'tour' | 'unknown'
    describe_blocks: list[str] = field(default_factory=list)
    test_names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    mock_models: list[str] = field(default_factory=list)
    line: int | None = None


@dataclass
class TestParseResult:
    """Output of parser_test.parse_module() for a single addon module.

    Carries all extracted test classes + helpers. JS suites are an empty-able
    list here; WI-3 populates them after its parser runs.
    """
    __test__: ClassVar[bool] = False  # M5: pytest must not collect this dataclass

    module: "ModuleInfo"         # forward ref OK — same file
    test_classes: list[TestClassInfo] = field(default_factory=list)
    test_helpers: list[TestHelperInfo] = field(default_factory=list)
    js_suites: list[JsTestSuiteInfo] = field(default_factory=list)
