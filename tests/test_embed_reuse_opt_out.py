# SPDX-License-Identifier: AGPL-3.0-or-later
"""``index-repo --no-embed-reuse`` forces a re-embed of what the run writes (no DB).

Unchanged chunk texts reuse their stored vector (ADR-0007 W14 note 3), so
``--full`` alone no longer re-embeds anything. When stored vectors may be
wrong although their text and model stamp match (the embedder's numerics
changed under the same model tag, suspect vectors, a text truncated before
``num_ctx`` grew) the operator needs a way to embed again.

Business rules protected here:

- ``--no-embed-reuse`` reaches ``write_module_embeddings(reuse=False)`` from
  the CLI, through ``index_all`` / ``index_profile`` / ``_index_repo``, in the
  sequential and the parallel paths; without the flag reuse stays on.
- The Web UI "reset embed" button, which promises a re-embed, passes it
  (``tests/test_web_ui_reset_embed.py``, PostgreSQL); that every chunk then
  goes to the embedder is ``tests/test_embedding_reuse_unit.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.indexer.__main__ as main_mod
from src.indexer import pipeline as pipeline_mod


@pytest.fixture
def cli_calls(monkeypatch):
    calls: dict[str, list[dict]] = {"profile": [], "all": []}
    lifecycle = {
        "versions": [], "gates_tripped": [], "undecidable": [], "errors": [],
        "deferred_presence": {}, "reports": [], "needs_attention": False,
    }

    def fake_profile(pg, **kw):
        calls["profile"].append(kw)
        return {"modules": 0, "lifecycle": dict(lifecycle)}

    def fake_all(pg, **kw):
        calls["all"].append(kw)
        return {"profiles_ok": 1, "modules": 0, "lifecycle": dict(lifecycle)}

    monkeypatch.setattr(main_mod, "open_production_pg", lambda: MagicMock())
    monkeypatch.setattr(main_mod, "index_profile", fake_profile)
    monkeypatch.setattr(main_mod, "index_all", fake_all)
    return calls


@pytest.mark.parametrize(
    ("scope", "key"), [(["--profile", "p1"], "profile"), (["--all"], "all")],
    ids=["profile", "all"],
)
def test_the_cli_flag_turns_reuse_off_and_reuse_is_on_by_default(cli_calls, scope, key):
    assert main_mod.main(["index-repo", *scope, "--no-embed"]) == 0
    assert main_mod.main(["index-repo", *scope, "--no-embed", "--no-embed-reuse"]) == 0

    default, opted_out = cli_calls[key]
    assert default["embed_reuse"] is True
    assert opted_out["embed_reuse"] is False


def _mock_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)
    conn.cursor.return_value = cur
    return conn


def _counters() -> dict:
    return dict.fromkeys(("modules", "views", "qweb", "embeddings", "js_patches", "owl_comps"), 0)


@pytest.mark.parametrize("workers", [1, 2], ids=["sequential", "parallel"])
@pytest.mark.parametrize("reuse", [True, False])
def test_index_all_and_index_profile_hand_the_choice_to_every_repo(workers, reuse):
    seen: list[bool] = []
    store = MagicMock()
    store.list_profiles.return_value = [{"name": "p1", "odoo_version": "99.0"}]
    store.get_repos_for_profile.return_value = [
        {"id": i, "local_path": f"/fake/{i}", "odoo_version": "99.0",
         "url": f"file://{i}", "branch": "99.0"} for i in (1, 2)
    ]
    store.get_ancestor_profile_names.return_value = ["p1"]

    def fake_index_repo(repo, writer, **kw):
        seen.append(kw["embed_reuse"])
        return _counters()

    with (
        patch("src.indexer.pipeline.repo_store", return_value=store),
        patch("src.indexer.pipeline.Neo4jWriter", return_value=MagicMock()),
        patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
        patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo),
        patch("src.indexer.pipeline.open_production_pg", side_effect=_mock_conn),
        patch("src.indexer.pipeline.run_lifecycle_reconcile"),
        patch("src.indexer.seed_patterns.run", return_value={"skipped": True}),
    ):
        pipeline_mod.index_all(
            _mock_conn(), max_workers=workers, profile_workers=workers, embed_reuse=reuse,
        )

    assert seen == [reuse, reuse]


@pytest.mark.parametrize("reuse", [True, False])
def test_index_repo_hands_the_choice_to_the_embedding_write(tmp_path, reuse):
    import contextlib

    import src.indexer.pipeline as _pipeline
    import src.indexer.writer_pgvector as wp
    from src.indexer.pipeline import _index_repo
    from tests.test_pipeline_refresh import (
        _add_module,
        _head,
        _install_inmemory_parsers,
        _make_repo,
    )

    repo = _make_repo(tmp_path / "addons", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    _add_module(repo, "mod_new")
    store = MagicMock()
    store.get_repo_head_sha.return_value = sha_a
    writes: list[tuple[str, bool]] = []

    def record_write(module, version, chunks, embedder, **kw):
        writes.append((module, kw["reuse"]))
        return 0

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=store))
        _install_inmemory_parsers(stack, {})
        stack.enter_context(patch("src.db.migrate._vector_extension_available", return_value=True))
        stack.enter_context(patch.object(wp, "make_chunks", return_value=[]))
        stack.enter_context(patch.object(wp, "write_module_embeddings", side_effect=record_write))
        stack.enter_context(
            patch("src.indexer.cross_repo.find_dependent_modules", return_value={})
        )
        _index_repo(
            {"id": 3, "local_path": str(repo), "odoo_version": "17.0",
             "url": "file://addons", "branch": "17.0", "ssh_key_id": None},
            MagicMock(), pg_conn=object(), embedder=object(), refresh=False,
            embed_reuse=reuse,
        )

    assert writes == [("mod_new", reuse)]
