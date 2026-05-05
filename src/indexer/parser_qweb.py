import xml.etree.ElementTree as ET
from pathlib import Path

from .models import ModuleInfo, QWebInfo, ViewParseResult


def _parse_template(elem: ET.Element, module: ModuleInfo) -> QWebInfo | None:
    """Extract QWebInfo from a <template> element.

    Returns None if template has no id attribute.
    """
    template_id = elem.get("id", "").strip()
    if not template_id:
        return None

    inherit_xmlid = elem.get("inherit_id", "").strip() or None
    return QWebInfo(
        xmlid=f"{module.name}.{template_id}",
        module=module.name,
        odoo_version=module.odoo_version,
        inherit_xmlid=inherit_xmlid,
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[QWebInfo]:
    """Parse a single XML file and extract all <template> elements.

    Returns a list of QWebInfo objects found in the file.
    Returns empty list if XML is malformed.
    """
    try:
        tree = ET.parse(filepath)
    except ET.ParseError:
        return []

    root = tree.getroot()
    qweb = []
    for tmpl in root.iter("template"):
        q = _parse_template(tmpl, module)
        if q:
            qweb.append(q)
    return qweb


def parse_module(module_info: ModuleInfo) -> ViewParseResult:
    """Parse all XML files in a module directory.

    Scans the module directory recursively, skipping common non-content directories.
    Returns a ViewParseResult with all QWeb templates found.
    """
    result = ViewParseResult(module=module_info)
    module_path = Path(module_info.path)

    # Directories to skip when scanning
    SKIP_DIRS = {".git", "static", "tests", "__pycache__"}

    for xml_file in sorted(module_path.rglob("*.xml")):
        # Skip files in excluded directories
        if SKIP_DIRS & set(xml_file.parts):
            continue
        result.qweb.extend(parse_file(str(xml_file), module_info))

    return result
