# src/indexer/models.py
from dataclasses import dataclass, field


@dataclass
class ModuleInfo:
    """Thông tin về một Odoo module."""
    name: str
    odoo_version: str
    repo: str
    path: str
    depends: list[str]
    version_raw: str = ""


@dataclass
class FieldInfo:
    """Thông tin về một Odoo field."""
    name: str
    ttype: str
    related: str | None = None
    compute: str | None = None
    stored: bool = True
    required: bool = False


@dataclass
class MethodInfo:
    """Thông tin về một method trong Odoo model."""
    name: str
    has_super_call: bool = False
    decorators: list[str] = field(default_factory=list)


@dataclass
class ModelInfo:
    """Thông tin về một Odoo model."""
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
    """Kết quả parse một module: module info + danh sách models."""
    module: ModuleInfo
    models: list[ModelInfo] = field(default_factory=list)


@dataclass
class XPathInfo:
    """XPath modification trong một extension view."""
    expr: str
    position: str  # before | after | inside | replace | attributes


@dataclass
class ViewInfo:
    """Thông tin về một Odoo ir.ui.view record."""
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
    """Thông tin về một QWeb template."""
    xmlid: str           # "module.template_id"
    module: str
    odoo_version: str
    inherit_xmlid: str | None = None


@dataclass
class ViewParseResult:
    """Kết quả parse XML files trong một module."""
    module: ModuleInfo
    views: list[ViewInfo] = field(default_factory=list)
    qweb: list[QWebInfo] = field(default_factory=list)
