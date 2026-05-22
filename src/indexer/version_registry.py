# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/version_registry.py — Shared version-dispatch registry (ADR-0032).
#
# Contract: a VersionRegistry is a sorted list of entries
#   (min_major: int, max_major: int | None, handler: T)
# where:
#   - min_major: first Odoo major for which this entry applies (inclusive).
#   - max_major: last Odoo major for which this entry applies (inclusive),
#     or None meaning "this version and all newer ones" (open-ended).
#   - handler: the callable or value associated with this range.
#
# Resolution:
#   - Entries are evaluated in ascending min_major order.
#   - First matching entry wins (no fall-through to a later entry).
#   - A version that matches no entry returns the default (None or caller-supplied).
#
# Adding v20 is a 1-line append:
#   entries.append((20, None, v20_handler))
#
# No plugin framework — this is intentionally minimal.


class VersionRegistry[T]:
    """Sorted (min_major, max_major|None, handler) registry.

    Entries are stored sorted by min_major ascending. First match wins.
    max_major=None means open-ended (applies to major and above).
    """

    def __init__(self, entries: list[tuple[int, int | None, T]]) -> None:
        # Sort by min_major ascending so iteration is deterministic.
        self._entries: list[tuple[int, int | None, T]] = sorted(
            entries, key=lambda e: e[0]
        )

    def resolve(self, major: int, default: T | None = None) -> T | None:
        """Return the handler for the given Odoo major version, or *default*."""
        for min_major, max_major, handler in self._entries:
            if major < min_major:
                continue
            if max_major is not None and major > max_major:
                continue
            return handler
        return default

    def resolve_version(self, odoo_version: str, default: T | None = None) -> T | None:
        """Parse *odoo_version* (e.g. ``"17.0"``) and delegate to :meth:`resolve`.

        Unparseable versions return *default* without raising.
        """
        try:
            major = int(str(odoo_version).split(".")[0])
        except (ValueError, IndexError, AttributeError):
            return default
        return self.resolve(major, default)


def make_version_registry[T](entries: list[tuple[int, int | None, T]]) -> VersionRegistry[T]:
    """Convenience constructor — mirrors ``VersionRegistry(entries)``."""
    return VersionRegistry(entries)
