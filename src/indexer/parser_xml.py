import xml.etree.ElementTree as ET
from pathlib import Path

from .models import ModuleInfo, ViewInfo, ViewParseResult, XPathInfo

_VIEW_TYPES = {
    "form", "tree", "list", "kanban", "search",
    "pivot", "graph", "calendar", "gantt", "activity", "map",
}


def _parse_record(record: ET.Element, module: ModuleInfo) -> ViewInfo | None:
    """Parse một <record> element làm ir.ui.view."""
    if record.get("model") != "ir.ui.view":
        return None

    xml_id = record.get("id", "").strip()
    if not xml_id:
        return None

    name = ""
    model = ""
    inherit_xmlid = None
    view_type = "form"
    mode = "primary"
    xpaths: list[XPathInfo] = []

    for child in record:
        if child.tag != "field":
            continue
        fname = child.get("name", "")
        if fname == "name":
            name = (child.text or "").strip()
        elif fname == "model":
            model = (child.text or "").strip()
        elif fname == "inherit_id":
            ref = child.get("ref", "").strip()
            if ref:
                inherit_xmlid = ref
                mode = "extension"
        elif fname == "arch":
            arch_children = list(child)
            if arch_children:
                first = arch_children[0]
                # Unwrap <data> container used by many extension views
                if first.tag == "data":
                    data_children = list(first)
                    if data_children and data_children[0].tag in _VIEW_TYPES:
                        view_type = data_children[0].tag
                elif first.tag in _VIEW_TYPES:
                    view_type = first.tag
            for xpath_el in child.iter("xpath"):
                expr = xpath_el.get("expr", "").strip()
                position = xpath_el.get("position", "inside").strip()
                if expr:
                    xpaths.append(XPathInfo(expr=expr, position=position))

    if not model:
        return None

    return ViewInfo(
        xmlid=f"{module.name}.{xml_id}",
        name=name,
        model=model,
        module=module.name,
        odoo_version=module.odoo_version,
        view_type=view_type,
        mode=mode,
        inherit_xmlid=inherit_xmlid,
        xpaths=xpaths,
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[ViewInfo]:
    """Parse một file XML, trả về list ViewInfo tìm được."""
    try:
        tree = ET.parse(filepath)
    except ET.ParseError:
        return []
    root = tree.getroot()
    views = []
    for record in root.iter("record"):
        view = _parse_record(record, module)
        if view:
            views.append(view)
    return views


def parse_module(module_info: ModuleInfo) -> ViewParseResult:
    """Parse toàn bộ file XML trong một module directory."""
    result = ViewParseResult(module=module_info)
    module_path = Path(module_info.path)
    SKIP_DIRS = {".git", "static", "tests", "__pycache__"}
    for xml_file in sorted(module_path.rglob("*.xml")):
        if SKIP_DIRS & set(xml_file.parts):
            continue
        result.views.extend(parse_file(str(xml_file), module_info))
    return result
