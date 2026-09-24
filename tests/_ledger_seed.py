# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed helpers for the Web UI repo/profile removal tests (ADR-0056 B10).

A removal is decided from three stores that the real indexer keeps in step:
the Neo4j graph (written here by the REAL ``_index_repo`` over an on-disk repo,
see ``tests/_retirement_fixture.py``), the ``module_presence`` ledger (seeded
through the public ``ModulePresenceStore`` API exactly as an index run commits
a scan) and pgvector embeddings (written through ``write_module_embeddings``).

Nothing here re-implements the removal: the tests call the HTTP routes and
compare the three stores before and after.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import _retirement_fixture as fx
from tests.conftest import TEST_VERSION

V = TEST_VERSION
OTHER_V = "98.0"  # an embeddings row at another version must never be touched

_TEST_COMMON = '''\
from odoo.tests.common import TransactionCase


class TestAssistant(TransactionCase):

    def test_ask_returns_true(self):
        self.assertTrue(self.env["ai.assistant"].create({"name": "x"}).action_ask())
'''


def presence_store():
    from src.db.module_presence import ModulePresenceStore
    from src.db.pg import get_pool

    return ModulePresenceStore(get_pool())


def open_writer():
    from src.indexer.writer_neo4j import Neo4jWriter

    w = Neo4jWriter(
        os.environ["NEO4J_URI"], os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"],
    )
    w.setup_indexes()
    return w


def add_profile(name: str, *, tenant_id: int | None = None, version: str = V) -> int:
    from src.db.pg import get_pool, repo_store

    pid = repo_store().add_profile(name=name, odoo_version=version)
    if tenant_id is not None:
        with get_pool().checkout() as c, c.cursor() as cur:
            cur.execute("UPDATE profiles SET tenant_id = %s WHERE id = %s", (tenant_id, pid))
    return pid


def add_repo(profile_id: int, local_path: Path | str, *, tenant_id: int | None = None) -> int:
    from src.db.pg import repo_store

    local_path = str(local_path)
    return repo_store().add_repo(
        profile_id=profile_id,
        url=f"https://git.example.com/{Path(local_path).parent.name}/{Path(local_path).name}.git",
        branch=V, local_path=local_path, tenant_id=tenant_id,
    )


def write_repo(root: Path, profile: str, basename: str, *, modules: tuple[str, ...],
               viin_ai_tests: bool = False) -> Path:
    """Write a git repo at ``root/<profile>/<basename>`` shipping *modules*.

    Known module names: ``viin_ai`` and ``viin_ai_rag`` (the full-artifact
    module of the retirement fixture); any other name gets a one-model module.
    """
    repo_dir = root / profile / basename
    repo_dir.mkdir(parents=True)
    for mod in modules:
        if mod == fx.SURVIVOR:
            fx.write_viin_ai(repo_dir)
            if viin_ai_tests:
                fx._write(repo_dir / mod / "tests" / "__init__.py",
                          "from . import test_assistant\n")
                fx._write(repo_dir / mod / "tests" / "test_assistant.py", _TEST_COMMON)
        elif mod == fx.RETIRED:
            fx.write_viin_ai_rag(repo_dir)
        else:
            write_simple_module(repo_dir, mod)
    fx.git_init_commit(repo_dir)
    return repo_dir


def write_simple_module(repo_dir: Path, name: str) -> None:
    model = name.replace("_", ".")
    fx._write(repo_dir / name / "__init__.py", "from . import models\n")
    fx._write(repo_dir / name / "__manifest__.py", fx._manifest(name, ["base"]))
    fx._write(repo_dir / name / "models" / "__init__.py", "from . import thing\n")
    fx._write(
        repo_dir / name / "models" / "thing.py",
        "from odoo import fields, models\n\n\n"
        f"class Thing(models.Model):\n    _name = \"{model}\"\n"
        f"    _description = \"{name}\"\n\n    name = fields.Char()\n",
    )


def index_graph(writer, repo_dir: Path, *, profile: str, repo_id: int) -> None:
    """Write the repo's nodes with the real indexer (graph only, no ledger)."""
    with pytest.MonkeyPatch.context() as mp:
        fx.index_repo_dir(writer, mp, repo_dir, profile=profile, repo_id=repo_id)


def observe(repo_id: int, profile: str, names, *, head: str, synced: bool = True,
            excluded: dict[str, str] | None = None) -> None:
    """Commit a scan of *names* for the repo at *head*, as an index run does.

    ``synced`` also records that the ledger reflects *head* (``head_sha`` ==
    ``presence_head_sha``), which is what makes the repo a decided owner.
    """
    from src.db.module_presence import ObservedModule
    from src.db.pg import repo_store

    observed = [
        ObservedModule(name=n, path=n, manifest_file=f"{n}/__manifest__.py") for n in names
    ] + [
        ObservedModule(name=n, path=n, manifest_file=f"{n}/__manifest__.py",
                       state="excluded", exclusion_reason=r)
        for n, r in (excluded or {}).items()
    ]
    presence_store().commit_observed(
        repo_id, profile_name=profile, odoo_version=V, head_sha=head, observed=observed,
    )
    repo_store().update_repo_head_sha(repo_id, head)
    if synced:
        presence_store().mark_presence_synced(repo_id, head)


def seed_embedding(module: str, profile: str, *, version: str = V, entity: str | None = None):
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    entity = entity or f"{module}.entity"
    chunk = EmbeddingChunk(
        "method", module, version, entity, "x.model", f"{module}/models/x.py", 0,
        f"def {entity.split('.')[-1]}(self): pass", profile_name=profile,
    )
    write_module_embeddings(module, version, [chunk], FakeEmbedder(dim=1024),
                            profile_name=profile)


def embedding_groups(conn) -> dict[tuple[str, str, str], int]:
    """``{(module, version, profile): rows}`` at V and OTHER_V."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT module, odoo_version, profile_name, count(*) FROM embeddings "
            "WHERE odoo_version IN (%s, %s) GROUP BY 1, 2, 3",
            (V, OTHER_V),
        )
        return {(m, v, p): int(n) for m, v, p, n in cur.fetchall()}


def ledger_rows(conn, where: str = "TRUE", params: tuple = ()) -> list[dict]:
    from psycopg2.extras import RealDictCursor

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT * FROM module_presence WHERE {where} ORDER BY repo_basename, name, id",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def module_node(driver, name: str, version: str = V) -> dict | None:
    with driver.session() as s:
        row = s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN properties(m) AS p",
            n=name, v=version,
        ).single()
    return dict(row["p"]) if row else None


def attributed_profiles(driver, module: str) -> set[tuple[str, ...]]:
    """Distinct ``profile`` arrays on the Module and every node attributed to it."""
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (n) WHERE n.odoo_version = $v
              AND ((n:Module AND n.name = $m) OR (n.module = $m AND NOT n:AssetBundle))
            RETURN DISTINCT n.profile AS p
            """,
            v=V, m=module,
        ).data()
    return {tuple(r["p"] or ()) for r in rows}


def tests_by_repo(driver, module: str) -> dict[tuple[str, str | None], int]:
    with driver.session() as s:
        return {
            (r["lbl"], r["repo"]): r["n"] for r in s.run(
                """
                MATCH (t {odoo_version: $v, module: $m})
                WHERE t:TestClass OR t:TestMethod
                RETURN labels(t)[0] AS lbl, t.repo AS repo, count(*) AS n
                """,
                v=V, m=module,
            ).data()
        }


def subtree(driver, module: str) -> dict[str, int]:
    """{label: count} of the nodes attributed to *module* (AssetBundle excluded)."""
    counts = fx.labels_with_module(driver, module)
    counts.pop("AssetBundle", None)  # version-global, reclaimed by its own GC
    lv = fx.lint_violations_of(driver, module)
    if lv:
        counts["LintViolation"] = lv
    return counts


def graph_snapshot(driver) -> list[tuple]:
    """Every node at V as (labels, sorted properties) minus volatile timestamps."""
    with driver.session() as s:
        rows = s.run(
            "MATCH (n) WHERE n.odoo_version = $v RETURN labels(n) AS l, properties(n) AS p",
            v=V,
        ).data()
    out = []
    for r in rows:
        props = {k: (tuple(v) if isinstance(v, list) else str(v)) for k, v in r["p"].items()}
        out.append((tuple(sorted(r["l"])), tuple(sorted(props.items()))))
    return sorted(out)


def shared_framework_nodes(driver) -> int:
    with driver.session() as s:
        return s.run(
            "MATCH (n) WHERE n.odoo_version = $v "
            "AND n.module IN ['@framework', '__unresolved__'] RETURN count(n) AS n",
            v=V,
        ).single()["n"]
