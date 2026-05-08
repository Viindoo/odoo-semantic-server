# src/indexer/models.py
from dataclasses import dataclass, field


@dataclass
class ModuleInfo:
    """Info for a single Odoo module."""
    name: str
    odoo_version: str
    repo: str
    path: str
    depends: list[str]
    version_raw: str = ""


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


@dataclass
class MethodInfo:
    """Info for a method on an Odoo model."""
    name: str
    has_super_call: bool = False
    decorators: list[str] = field(default_factory=list)
    source_code: str | None = None  # raw method source, for embedding


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


@dataclass
class QWebInfo:
    """Info for a single QWeb template."""
    xmlid: str           # "module.template_id"
    module: str
    odoo_version: str
    inherit_xmlid: str | None = None
    content: str | None = None    # serialized XML content of <template>, for embedding
    file_path: str | None = None  # source XML file path


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
    """Parse result for XML files in a module: views + qweb templates."""
    module: ModuleInfo
    views: list[ViewInfo] = field(default_factory=list)
    qweb: list[QWebInfo] = field(default_factory=list)


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


# --- Spec layer (M4.5, per ADR-0002) ----------------------------------------

@dataclass
class CoreSymbolInfo:
    """An Odoo upstream API entity, captured per version.

    Composite key: (qualified_name, odoo_version) — see ADR-0002 §1.
    `kind` ∈ {function, class, decorator, exception, field_type, orm_method, cursor_method}.
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
