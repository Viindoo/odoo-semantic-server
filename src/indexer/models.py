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


@dataclass
class MethodInfo:
    """Info for a method on an Odoo model."""
    name: str
    has_super_call: bool = False
    decorators: list[str] = field(default_factory=list)


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


@dataclass
class QWebInfo:
    """Info for a single QWeb template."""
    xmlid: str           # "module.template_id"
    module: str
    odoo_version: str
    inherit_xmlid: str | None = None


@dataclass
class ViewParseResult:
    """Parse result for XML files in a module: views + qweb templates."""
    module: ModuleInfo
    views: list[ViewInfo] = field(default_factory=list)
    qweb: list[QWebInfo] = field(default_factory=list)
