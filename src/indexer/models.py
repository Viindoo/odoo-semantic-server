# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/models.py
from dataclasses import dataclass, field
from pathlib import Path


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
