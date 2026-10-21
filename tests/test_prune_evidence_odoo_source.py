# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-checkout check of the prune source oracle (ADR-0057) - OPT-IN, bounded.

Rule: everything the REAL OSM parsers produce from a real Odoo module must be
judged ``present`` by the source oracle (never ``gone``). The default lane
covers this with synthetic fixtures (``tests/test_prune_evidence.py``); this
file samples a fixed handful of real modules per available checkout as a
second line of evidence.

It is NOT part of the default lane: it runs only when selected explicitly,

    pytest -m odoo_source tests/test_prune_evidence_odoo_source.py

with checkouts resolved by ``tests/_odoo_checkouts.py::checkout_root`` (
``OSM_ODOO_CHECKOUTS`` / ``ODOO<N>_SRC``), each a git work tree. Bounded to
``_MODULES`` x the newest ``_MAX_VERSIONS`` available majors (target < 60 s).

The exhaustive real-data check is the nightly run itself: the oracle judges
every stale entity of every re-parsed module in production, and whatever it
keeps lands in ``lifecycle-audit`` as ``parser_gaps`` (one row per module, with
a sample) - a non-empty finding there is either a real parser gap or an oracle
pattern that is too broad, never a deletion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.indexer import prune_evidence as pe
from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root
from tests.test_prune_evidence import _assert_nothing_produced_is_gone, _parsed_items

pytestmark = pytest.mark.odoo_source

_MODULES = ("base", "sale", "account")
_MAX_VERSIONS = 2  # measured: base+sale+account x 3 majors ~ 69 s, x 2 well under 60 s


@pytest.fixture(autouse=True)
def _opt_in(request, monkeypatch):
    if "odoo_source" not in (request.config.getoption("-m") or ""):
        pytest.skip("opt-in: run with -m odoo_source")
    # Service-owned clones (e.g. the indexer's own) need safe.directory; this
    # is process-local, never the user's git config.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")


def _available() -> list[tuple[int, Path]]:
    found = [(m, root) for m in SURVEYED_MAJORS if (root := checkout_root(m)) is not None]
    return found[-_MAX_VERSIONS:]


def _module_dir(root: Path, name: str) -> str | None:
    for rel in (f"odoo/addons/{name}", f"openerp/addons/{name}", f"addons/{name}"):
        if (root / rel).is_dir():
            return rel
    return None


@pytest.mark.parametrize("module", _MODULES)
def test_every_entity_the_parsers_produce_from_a_real_module_is_present(module):
    from src.indexer.models import ModuleInfo
    from src.indexer.registry import parse_manifest

    checkouts = _available()
    if not checkouts:
        pytest.skip("no Odoo checkout resolved (OSM_ODOO_CHECKOUTS / ODOO<N>_SRC)")
    checked = 0
    for major, root in checkouts:
        rel = _module_dir(root, module)
        if rel is None:
            continue
        manifest_path = next(
            (root / rel / m for m in ("__manifest__.py", "__openerp__.py")
             if (root / rel / m).is_file()), None,
        )
        manifest = parse_manifest(str(manifest_path)) if manifest_path else {}
        info = ModuleInfo(
            name=module, odoo_version=f"{major}.0", repo=root.name, path=str(root / rel),
            depends=list(manifest.get("depends") or []), repo_root=root,
        )
        source = pe.load_module_source(root, module, rel)
        assert source.available, f"{module}@{major}.0: {source.reason}"
        nodes, rels = _parsed_items(info, module=module)
        assert nodes, f"{module}@{major}.0 produced nothing - wrong path?"
        _assert_nothing_produced_is_gone(source, nodes, rels)
        checked += 1
    if not checked:
        pytest.skip(f"{module} is in none of the resolved checkouts")
