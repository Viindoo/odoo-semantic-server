"""Walk Odoo addon roots and parse each ``__manifest__.py`` statically.

Uses ``ast.literal_eval`` — never ``eval`` or ``exec`` — because
``__manifest__.py`` is by Odoo convention a single Python expression that
evaluates to a dict literal.  This keeps parsing sandboxed even when
processing untrusted third-party addons.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

_FILTERED_MODULES = frozenset({"studio_customization"})

_MANIFEST_FILENAMES = ("__manifest__.py", "__openerp__.py")


@dataclass(frozen=True)
class ManifestRecord:
    """Parsed, normalised representation of a single ``__manifest__.py``."""

    name: str
    path: Path
    depends: tuple[str, ...]
    auto_install: bool | tuple[str, ...]
    version: str
    category: str
    application: bool
    installable: bool


def _read_manifest(manifest_path: Path) -> dict[object, object] | None:
    """Return the parsed manifest dict or ``None`` on any parse error."""
    try:
        source = manifest_path.read_text(encoding="utf-8")
        result = ast.literal_eval(source)
    except (OSError, ValueError, SyntaxError) as exc:
        _logger.warning("manifest parse error %s: %s", manifest_path, exc)
        return None
    if not isinstance(result, dict):
        _logger.warning("manifest is not a dict: %s", manifest_path)
        return None
    return result


def _normalise_auto_install(
    raw: object,
    depends: tuple[str, ...],
) -> bool | tuple[str, ...]:
    """Coerce the ``auto_install`` manifest value to a typed form.

    Odoo core (``load_manifest``) converts ``True`` → ``set(depends)``
    internally, but we preserve the typed distinction so callers can see
    whether the module declared an explicit trigger set or used the
    shorthand.  ``False`` / absent → ``False``.  An iterable of names
    → ``tuple[str, ...]``.
    """
    if raw is True:
        return True
    if raw is False or raw is None:
        return False
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(x) for x in raw)
    _logger.warning("unexpected auto_install value %r; treating as False", raw)
    return False


def scan_addon_root(root: Path) -> list[ManifestRecord]:
    """Return ``ManifestRecord``s for every installable addon under *root*.

    Skips directories that:
    - contain no recognisable manifest file,
    - have ``installable`` set to ``False``,
    - are in the hard-filtered set (``studio_customization``).
    """
    records: list[ManifestRecord] = []
    if not root.is_dir():
        _logger.warning("addon root does not exist: %s", root)
        return records

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in _FILTERED_MODULES:
            continue

        manifest_path: Path | None = None
        for filename in _MANIFEST_FILENAMES:
            candidate = entry / filename
            if candidate.is_file():
                manifest_path = candidate
                break

        if manifest_path is None:
            continue

        raw = _read_manifest(manifest_path)
        if raw is None:
            continue

        installable = bool(raw.get("installable", True))
        if not installable:
            continue

        depends_raw = raw.get("depends", [])
        if not isinstance(depends_raw, (list, tuple)):
            _logger.warning("module %s: depends is not a list; skipping", name)
            continue
        depends = tuple(str(d) for d in depends_raw)

        auto_install_raw = raw.get("auto_install", False)
        auto_install = _normalise_auto_install(auto_install_raw, depends)

        records.append(
            ManifestRecord(
                name=name,
                path=manifest_path,
                depends=depends,
                auto_install=auto_install,
                version=str(raw.get("version", "")),
                category=str(raw.get("category", "")),
                application=bool(raw.get("application", False)),
                installable=installable,
            )
        )

    return records


def scan_addon_roots(roots: list[Path]) -> list[ManifestRecord]:
    """Scan multiple addon roots and return a deduplicated list of records.

    When the same module name appears in multiple roots, the first root wins
    (roots are consulted in the order provided, matching Odoo's addons-path
    precedence).
    """
    seen: set[str] = set()
    all_records: list[ManifestRecord] = []
    for root in roots:
        for record in scan_addon_root(root):
            if record.name not in seen:
                seen.add(record.name)
                all_records.append(record)
    return all_records
