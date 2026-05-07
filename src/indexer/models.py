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
