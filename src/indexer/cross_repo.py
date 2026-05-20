# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/cross_repo.py
"""Cross-repo dependency change propagation (M7 W14).

When an incremental indexer run detects changed modules in repo A, any other
repos whose modules have DEPENDS_ON edges into those changed modules must be
re-indexed on the next run — their API surface may have silently changed.

This module provides the Neo4j query that finds such dependent repos.
"""
import logging

_logger = logging.getLogger(__name__)

_CYPHER_FIND_DEPENDENT_REPOS = """
MATCH (dependent:Module {odoo_version: $version})-[:DEPENDS_ON]->
      (changed:Module {odoo_version: $version})
WHERE changed.name IN $changed_names AND NOT dependent.name IN $changed_names
RETURN DISTINCT dependent.repo AS repo
"""


def find_dependent_repos(
    driver,
    odoo_version: str,
    changed_module_names: set[str],
) -> list[str]:
    """Return list of repo identifiers (m.repo property values from Neo4j) of
    Modules at this version that DEPENDS_ON any module in changed_module_names,
    EXCLUDING those that own a changed module themselves (don't reset the
    repo we just indexed).

    Args:
        driver:               Neo4j driver (already open).
        odoo_version:         Odoo version string, e.g. "17.0".
        changed_module_names: Set of module names that changed in this run.

    Returns:
        List of distinct repo identifiers (the ``m.repo`` property value,
        which equals ``Path(local_path).name`` as stored in Neo4j by the
        indexer). Empty list when no dependent repos are found or
        changed_module_names is empty.
    """
    if not changed_module_names:
        return []

    with driver.session() as session:
        result = session.run(
            _CYPHER_FIND_DEPENDENT_REPOS,
            version=odoo_version,
            changed_names=list(changed_module_names),
        )
        return [row["repo"] for row in result.data() if row["repo"] is not None]
