# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temp git repos + observation helpers for the module lifecycle tests (ADR-0056).

A ``GitRepo`` is a work clone with a bare ``origin``: the nightly index job
fetches and resets to ``origin/<branch>`` and trusts a scan only when HEAD is
that ref, so every commit is pushed unless a test wants an untrusted checkout.
Modules are minimal but real Odoo modules (manifest + one model with a field
and a method), so a run writes a Module, its children and its embeddings.
"""
from __future__ import annotations

import subprocess
import textwrap
from collections import defaultdict
from pathlib import Path

from src.db.pg import repo_store
from src.indexer.embedder import FakeEmbedder
from src.indexer.pipeline import index_profile
from tests.conftest import TEST_VERSION

V = TEST_VERSION


# ---------------------------------------------------------------------------
# Temp git repos with a bare origin (the nightly job resets to origin/<branch>)
# ---------------------------------------------------------------------------

def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


class GitRepo:
    """A work clone + its bare origin. ``commit`` pushes unless told not to."""

    def __init__(self, parent: Path, name: str, branch: str = V) -> None:
        parent.mkdir(parents=True, exist_ok=True)
        self.branch = branch
        self.origin = parent / f"{name}.origin.git"
        self.path = parent / name
        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True,
                       capture_output=True)
        self.path.mkdir()
        run_git(self.path, "init")
        run_git(self.path, "checkout", "-b", branch)
        run_git(self.path, "config", "user.email", "dev@example.com")
        run_git(self.path, "config", "user.name", "Dev")
        run_git(self.path, "remote", "add", "origin", str(self.origin))
        (self.path / ".gitkeep").write_text("")
        self.commit("init")

    @property
    def url(self) -> str:
        return f"file://{self.origin}"

    def commit(self, message: str, *, push: bool = True, force: bool = False) -> str:
        run_git(self.path, "add", "-A")
        run_git(self.path, "commit", "--allow-empty", "-m", message)
        if push:
            args = ["push", "origin", f"HEAD:refs/heads/{self.branch}"]
            if force:
                args.insert(1, "--force")
            run_git(self.path, *args)
            run_git(self.path, "fetch", "origin")
        return self.head()

    def head(self) -> str:
        return run_git(self.path, "rev-parse", "HEAD")

    def mv(self, old: str, new: str) -> None:
        run_git(self.path, "mv", old, new)

    def rm(self, name: str) -> None:
        run_git(self.path, "rm", "-r", "-q", name)


def _model_py(name: str, *, model: str | None = None, inherit: str | None = None,
              extra_field: str = "") -> str:
    model = model or f"{name.replace('_', '.')}.record"
    head = f"_inherit = {inherit!r}" if inherit else f"_name = {model!r}"
    return textwrap.dedent(f"""\
        from odoo import fields, models


        class Record(models.Model):
            {head}
            _description = "{name}"

            label = fields.Char()
            {extra_field}

            def action_touch(self):
                return True
        """)


def write_module(repo: GitRepo, name: str, *, depends: list[str] | None = None,
                 installable: bool = True, version: str | None = None,
                 license: str = "LGPL-3", author: str = "Odoo S.A.",
                 model: str | None = None, inherit: str | None = None,
                 extra_field: str = "", old_technical_name: str | None = None) -> None:
    """A minimal real Odoo module: manifest + one model with a field and a method."""
    mod = repo.path / name
    (mod / "models").mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": version or f"{V}.1.0.0",
        "depends": depends or [],
        "installable": installable,
        "license": license,
        "author": author,
    }
    if old_technical_name:
        manifest["old_technical_name"] = old_technical_name
    (mod / "__manifest__.py").write_text(repr(manifest) + "\n")
    (mod / "__init__.py").write_text("from . import models\n")
    (mod / "models" / "__init__.py").write_text("from . import record\n")
    (mod / "models" / "record.py").write_text(
        _model_py(name, model=model, inherit=inherit, extra_field=extra_field)
    )


# ---------------------------------------------------------------------------
# Registration + runs
# ---------------------------------------------------------------------------

def register(profile: str, *repos: GitRepo) -> list[int]:
    """Profile + repos (ids in the given order). Profile is created on first use."""
    store = repo_store()
    existing = {p["name"]: p for p in store.list_profiles()}
    pid = existing[profile]["id"] if profile in existing else store.add_profile(profile, V)
    return [store.add_repo(pid, r.url, r.branch, str(r.path)) for r in repos]


def run(pg_conn, profile: str, **kw) -> dict:
    kw.setdefault("embedder", FakeEmbedder(dim=1024))
    return index_profile(pg_conn, profile_name=profile, **kw)


# ---------------------------------------------------------------------------
# Observations (graph, pgvector, ledger)
# ---------------------------------------------------------------------------

def module_node(driver, name: str) -> dict | None:
    with driver.session() as s:
        rec = s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN properties(m) AS p",
            n=name, v=V,
        ).single()
    return dict(rec["p"]) if rec else None


def children(driver, name: str) -> int:
    """Nodes (other than the Module) the index attributes to module *name*."""
    with driver.session() as s:
        return s.run(
            "MATCH (n) WHERE n.odoo_version = $v AND n.module = $m AND NOT n:Module "
            "RETURN count(n) AS n",
            v=V, m=name,
        ).single()["n"]


def child_profiles(driver, name: str) -> set[tuple[str, ...]]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (n) WHERE n.odoo_version = $v AND n.module = $m AND n.profile IS NOT NULL "
            "RETURN DISTINCT n.profile AS p",
            v=V, m=name,
        ).data()
    return {tuple(sorted(r["p"])) for r in rows}


def embeddings(pg_conn, name: str, profile: str | None = None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND module = %s "
            "AND (%s::text IS NULL OR profile_name = %s::text)",
            (V, name, profile, profile),
        )
        return cur.fetchone()[0]


def ledger(pg_conn, repo_id: int, name: str) -> dict | None:
    from psycopg2.extras import RealDictCursor
    with pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM module_presence WHERE repo_id = %s AND name = %s",
            (repo_id, name),
        )
        row = cur.fetchone()
    # A missing row reads as all-None so a test fails on its assertion, not on
    # a subscript of None.
    return defaultdict(lambda: None, row) if row else defaultdict(lambda: None)


def repo_row(pg_conn, repo_id: int) -> dict:
    from psycopg2.extras import RealDictCursor
    with pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM repos WHERE id = %s", (repo_id,))
        return dict(cur.fetchone())


def assert_gone(driver, pg_conn, name: str) -> None:
    assert module_node(driver, name) is None, f"{name}: Module node must be retired"
    assert children(driver, name) == 0, f"{name}: every child node must be retired"
    assert embeddings(pg_conn, name) == 0, f"{name}: every embedding row must be retired"


def assert_live(driver, pg_conn, name: str) -> None:
    assert module_node(driver, name) is not None, f"{name}: Module node must stay"
    assert children(driver, name) > 0, f"{name}: its child nodes must stay"
    assert embeddings(pg_conn, name) > 0, f"{name}: its embeddings must stay"


def lc(summary: dict) -> dict:
    """The run's lifecycle block ({} when the run reports none)."""
    return summary.get("lifecycle") or {}


def needs_attention(summary: dict) -> bool:
    return bool(lc(summary).get("needs_attention"))
