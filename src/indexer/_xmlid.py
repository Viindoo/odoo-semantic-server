# SPDX-License-Identifier: AGPL-3.0-or-later
def qualify_xmlid(raw: str | None, module_name: str) -> str | None:
    """Apply Odoo's external-id rule: a value containing '.' is already
    module-qualified; a bare value is prefixed with the current module.
    Returns None for empty/None input."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return raw if "." in raw else f"{module_name}.{raw}"
