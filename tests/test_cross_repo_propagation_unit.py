# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-repo dependency propagation re-writes the dependent MODULES, not whole repos (no DB).

Business rules protected here (ADR-0007 W14 note 3):

- A change to an upstream module flags, in the lifecycle ledger, only the
  modules of other repos that depend on it - directly or transitively - for a
  rewrite on their repo's next run. The other modules of those repos are not
  touched, and no repo loses its ``head_sha`` (that forced a full re-index
  with a full re-embed of every module of the repo).
- A transitive dependent is reached even through an unchanged module of the
  repo that was just indexed, or through a third repo.
- The repo that was just indexed never flags its own modules.
- A dependent repo without ledger rows at the version cannot be flagged per
  module: its ``head_sha`` is reset, as before the ledger existed. The same
  reset is the fallback when the run has no ledger or the flag write fails.
- The dependency walk ends on a dependency cycle.
"""
from __future__ import annotations

import logging

import pytest

from src.indexer import cross_repo
from src.indexer.cross_repo import find_dependent_modules, propagate_to_dependents

V = "99.0"


class _Graph:
    """``DEPENDS_ON`` edges at one version: module -> (repo basename, depends)."""

    def __init__(self, modules: dict[str, tuple[str, list[str]]]) -> None:
        self.modules = modules
        self.queries = 0

    def direct_dependents(self, frontier: list[str], seen: list[str]) -> list[dict]:
        self.queries += 1
        return [
            {"name": name, "repo": repo}
            for name, (repo, deps) in sorted(self.modules.items())
            if name not in seen and any(d in frontier for d in deps)
        ]


class _Driver:
    def __init__(self, graph: _Graph) -> None:
        self.graph = graph

    def session(self):
        graph = self.graph

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def data(self):
                return self._rows

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, query, *, version, frontier, seen):
                assert query == cross_repo._CYPHER_DIRECT_DEPENDENT_MODULES
                assert version == V
                return _Result(graph.direct_dependents(frontier, seen))

        return _Session()


class _Ledger:
    """``module_presence`` rows: (repo_id, name, state) at version V."""

    def __init__(self, rows: list[tuple[int, str, str]], *, fail: bool = False) -> None:
        self.rows = rows
        self.flagged: set[tuple[int, str]] = set()
        self.fail = fail

    def mark_dependents_needs_rewrite(self, odoo_version, names, *, exclude_repo_ids=()):
        if self.fail:
            raise RuntimeError("ledger lock retire:99.0 not acquired within 30s")
        assert odoo_version == V
        hit = sorted(
            (rid, name) for rid, name, state in self.rows
            if name in names and state == "present" and rid not in exclude_repo_ids
        )
        self.flagged.update(hit)
        return hit

    def repo_ids_without_rows(self, repo_ids, odoo_version):
        have = {rid for rid, _name, _state in self.rows}
        return [i for i in sorted(repo_ids) if i not in have]


class _Repos:
    """``repos`` rows: basename -> id at version V, and the head_sha resets."""

    def __init__(self, ids_by_basename: dict[str, int]) -> None:
        self.ids_by_basename = ids_by_basename
        self.reset: list[int] = []

    def get_repo_ids_by_local_path_basenames(self, basenames, odoo_version):
        assert odoo_version == V
        return [self.ids_by_basename[b] for b in basenames if b in self.ids_by_basename]

    def reset_head_sha(self, repo_ids):
        self.reset.extend(repo_ids)
        return len(repo_ids)


# The observed case (2026-10-24): tvtmaaddons changed to_git; one module of
# saas-infrastructure depends on it, a second one depends on that one, and the
# rest of saas-infrastructure has nothing to do with it.
GRAPH = {
    "to_git": ("tvtmaaddons", []),
    "to_base": ("tvtmaaddons", []),
    "to_git_ui": ("tvtmaaddons", ["to_git"]),
    "to_server_ssh_git": ("saas-infrastructure", ["to_git"]),
    "to_odoo_saas": ("saas-infrastructure", ["to_server_ssh_git"]),
    "to_dns": ("saas-infrastructure", ["to_base"]),
    "viin_saas_portal": ("erponline-enterprise", ["to_git_ui"]),
}
LEDGER_ROWS = [
    (31, "to_git", "present"), (31, "to_base", "present"), (31, "to_git_ui", "present"),
    (42, "to_server_ssh_git", "present"), (42, "to_odoo_saas", "present"),
    (42, "to_dns", "present"),
    (32, "viin_saas_portal", "present"),
]
REPOS = {"tvtmaaddons": 31, "saas-infrastructure": 42, "erponline-enterprise": 32}


def _propagate(changed, *, ledger=None, repos=None, graph=GRAPH):
    repos = repos if repos is not None else _Repos(REPOS)
    result = propagate_to_dependents(
        _Driver(_Graph(graph)), V, set(changed),
        exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
        presence=ledger, repo_store=repos,
    )
    return result, repos


def test_an_upstream_change_flags_only_the_dependent_modules_of_another_repo():
    ledger = _Ledger(LEDGER_ROWS)

    result, repos = _propagate({"to_git"}, ledger=ledger)

    assert result.marked[42] == ["to_odoo_saas", "to_server_ssh_git"]
    assert (42, "to_dns") not in ledger.flagged
    assert repos.reset == [], "no repo may lose its head_sha (full re-index)"


def test_a_transitive_dependent_is_reached_through_an_unchanged_module_of_the_indexed_repo():
    ledger = _Ledger(LEDGER_ROWS)

    result, _repos = _propagate({"to_git"}, ledger=ledger)

    # erponline's viin_saas_portal depends on tvtmaaddons' unchanged to_git_ui.
    assert result.marked[32] == ["viin_saas_portal"]


def test_the_indexed_repo_never_flags_its_own_modules():
    ledger = _Ledger(LEDGER_ROWS)

    result, _repos = _propagate({"to_git"}, ledger=ledger)

    assert 31 not in result.marked
    assert (31, "to_git_ui") not in ledger.flagged


def test_a_dependent_repo_without_ledger_rows_is_reset_instead():
    ledger = _Ledger([r for r in LEDGER_ROWS if r[0] != 42])

    result, repos = _propagate({"to_git"}, ledger=ledger)

    assert repos.reset == [42]
    assert result.reset_repo_ids == [42]
    assert result.marked == {32: ["viin_saas_portal"]}


def test_without_a_ledger_the_repos_of_the_direct_dependents_are_reset():
    result, repos = _propagate({"to_git"}, ledger=None)

    # saas-infrastructure ships a DIRECT dependent; erponline only a transitive one.
    assert repos.reset == [42]
    assert result.marked == {}


def test_a_failed_flag_write_falls_back_to_the_reset(caplog):
    ledger = _Ledger(LEDGER_ROWS, fail=True)

    with caplog.at_level(logging.WARNING, logger="src.indexer.cross_repo"):
        result, repos = _propagate({"to_git"}, ledger=ledger)

    assert repos.reset == [42]
    assert result.marked == {}
    assert "could not flag" in caplog.text


def test_a_change_nothing_depends_on_flags_and_resets_nothing():
    ledger = _Ledger(LEDGER_ROWS)

    result, repos = _propagate({"viin_saas_portal"}, ledger=ledger)

    assert result.dependents == 0
    assert ledger.flagged == set()
    assert repos.reset == []


def test_the_dependency_walk_ends_on_a_cycle():
    graph = {
        "a": ("r1", ["c"]),
        "b": ("r2", ["a"]),
        "c": ("r2", ["b"]),
    }

    found = find_dependent_modules(_Driver(_Graph(graph)), V, {"a"})

    assert found == {"b": ("r2", 1), "c": ("r2", 2)}


@pytest.mark.parametrize("changed", [set(), {"to_git"}])
def test_the_walk_reports_each_dependent_once_with_its_shortest_depth(changed):
    found = find_dependent_modules(_Driver(_Graph(GRAPH)), V, changed)

    if not changed:
        assert found == {}
        return
    assert found == {
        "to_git_ui": ("tvtmaaddons", 1),
        "to_server_ssh_git": ("saas-infrastructure", 1),
        "to_odoo_saas": ("saas-infrastructure", 2),
        "viin_saas_portal": ("erponline-enterprise", 2),
    }


def test_an_incremental_run_propagates_and_names_it_in_the_warning_log(tmp_path, caplog):
    """The wiring in ``_index_repo``: after an incremental write the dependents
    of the written modules are propagated to (the repo itself excluded), and
    one WARNING line - visible in the WARNING-level nightly log - names it."""
    import contextlib
    from unittest.mock import MagicMock, patch

    import src.indexer.pipeline as _pipeline
    from src.indexer.pipeline import _index_repo
    from tests.test_pipeline_refresh import (
        _add_module,
        _head,
        _install_inmemory_parsers,
        _make_repo,
    )

    repo = _make_repo(tmp_path / "upstream_repo", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    _add_module(repo, "mod_new")
    repo_row = {
        "id": 7, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://upstream", "branch": "17.0", "ssh_key_id": None,
    }
    fake_store = MagicMock()
    fake_store.get_repo_head_sha.return_value = sha_a
    fake_store.get_repo_ids_by_local_path_basenames.return_value = [99]
    fake_store.reset_head_sha.return_value = 1
    dependents = {
        "dep_mod": ("downstream_repo", 1),
        "own_dep": ("upstream_repo", 1),
    }

    with contextlib.ExitStack() as stack, caplog.at_level(logging.WARNING):
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        walk = stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_modules", return_value=dependents,
        ))
        _install_inmemory_parsers(stack, {})
        _index_repo(repo_row, MagicMock(), pg_conn=object(), refresh=False)

    assert walk.call_args.args[1:] == ("17.0", {"mod_new"})
    # No ledger in this run: the downstream repo of the direct dependent is
    # reset; the repo that was just indexed is not a candidate.
    fake_store.get_repo_ids_by_local_path_basenames.assert_called_once_with(
        ["downstream_repo"], "17.0",
    )
    fake_store.reset_head_sha.assert_called_once_with([99])
    line = next(r for r in caplog.records if "cross-repo dep propagation" in r.getMessage())
    assert line.levelno == logging.WARNING
    assert "mod_new" in line.getMessage() and "head_sha reset on 1 repo(s)" in line.getMessage()


# ---------------------------------------------------------------------------
# Robustness: a failed walk, a failed fallback, a stop (finding 2)
# ---------------------------------------------------------------------------


class _WalkFailsDriver:
    """The ``DEPENDS_ON`` closure walk raises (a Neo4j error); the one-hop
    ``find_dependent_repos`` query answers *direct_repos*, or raises too."""

    def __init__(self, direct_repos: list[str] | None) -> None:
        self.direct_repos = direct_repos

    def session(self):
        direct_repos = self.direct_repos

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def data(self):
                return self._rows

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, query, **_params):
                if query == cross_repo._CYPHER_DIRECT_DEPENDENT_MODULES:
                    raise RuntimeError("Neo4j TransientError: deadlock")
                assert query == cross_repo._CYPHER_FIND_DEPENDENT_REPOS
                if direct_repos is None:
                    raise RuntimeError("Neo4j ServiceUnavailable")
                return _Result([{"repo": r} for r in direct_repos])

        return _Session()


def test_a_failed_dependency_walk_resets_the_direct_dependents_repos(caplog):
    ledger = _Ledger(LEDGER_ROWS)
    repos = _Repos(REPOS)

    with caplog.at_level(logging.WARNING, logger="src.indexer.cross_repo"):
        result = propagate_to_dependents(
            _WalkFailsDriver(["saas-infrastructure", "tvtmaaddons"]), V, {"to_git"},
            exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
            presence=ledger, repo_store=repos,
        )

    assert result.walk_failed is True
    assert repos.reset == [42], "the indexed repo itself is never reset"
    assert result.reset_repo_ids == [42]
    assert ledger.flagged == set()
    assert "dependency walk failed" in caplog.text


def test_a_failed_fallback_raises_so_the_caller_keeps_its_head_sha():
    with pytest.raises(RuntimeError, match="ServiceUnavailable"):
        propagate_to_dependents(
            _WalkFailsDriver(None), V, {"to_git"},
            exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
            presence=_Ledger(LEDGER_ROWS), repo_store=_Repos(REPOS),
        )


def _run_index_repo(
    tmp_path, *, walk=None, bootstrap=(), meta_only=False, stack_extra=None, store=None,
):
    """``_index_repo`` on a real two-commit git repo (in-memory parsers, no DB).

    Commit A ships ``mod_old``; commit B adds ``mod_new`` (or, with
    *meta_only*, only touches a top-level file). ``repo_store`` reports
    head_sha A, so the run is incremental A..B. *walk* replaces
    ``cross_repo.find_dependent_modules`` (a callable side effect or a return
    value); *bootstrap* are the names the shared-module bootstrap re-parses.
    *store* (a MagicMock) stands for ``repo_store()``; pass one to inspect
    it after a raising run. Returns ``(fake_store, walk_mock, parsed_module_names)``.
    """
    import contextlib
    from unittest.mock import MagicMock, patch

    import src.indexer.pipeline as _pipeline
    import src.indexer.pipeline_repo as _pr
    from src.indexer.pipeline import _index_repo
    from tests.test_pipeline_refresh import (
        _add_module,
        _git,
        _head,
        _install_inmemory_parsers,
        _make_repo,
    )

    repo = _make_repo(tmp_path / "upstream_repo", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    if meta_only:
        (repo / "README.md").write_text("docs only\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "docs")
    else:
        _add_module(repo, "mod_new")
    repo_row = {
        "id": 7, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://upstream", "branch": "17.0", "ssh_key_id": None,
    }
    fake_store = store if store is not None else MagicMock()
    fake_store.get_repo_head_sha.return_value = sha_a
    fake_store.get_repo_ids_by_local_path_basenames.return_value = [99]
    fake_store.reset_head_sha.return_value = 1
    parsed: list[str] = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        walk_kw = (
            {"side_effect": walk} if callable(walk) or isinstance(walk, BaseException)
            else {"return_value": walk if walk is not None else {}}
        )
        walk_mock = stack.enter_context(
            patch("src.indexer.cross_repo.find_dependent_modules", **walk_kw)
        )
        _install_inmemory_parsers(stack, {})
        real_parse = _pr.parser_python.parse_module

        def record_parse(info, **kw):
            parsed.append(info.name)
            return real_parse(info, **kw)

        stack.enter_context(
            patch.object(_pr.parser_python, "parse_module", side_effect=record_parse)
        )
        stack.enter_context(
            patch.object(_pr, "shared_parse_bootstrap", return_value=list(bootstrap))
        )
        if stack_extra is not None:
            stack_extra(stack)
        _index_repo(repo_row, MagicMock(), pg_conn=object(), refresh=False)
    return fake_store, walk_mock, parsed


def test_propagation_runs_before_head_sha_advances_so_a_stop_loses_nothing(tmp_path):
    """A stop (SIGTERM) while the propagation runs must leave head_sha where it
    was: the next run repeats the diff and the propagation. With head_sha
    advanced first, the dependents were never flagged."""
    from unittest.mock import MagicMock

    from src.indexer.stop import IndexRunStopped

    store = MagicMock()
    with pytest.raises(IndexRunStopped):
        _run_index_repo(
            tmp_path, walk=IndexRunStopped("index run stop requested"), store=store,
        )

    store.update_repo_head_sha.assert_not_called()


def test_head_sha_is_still_old_while_the_dependents_are_flagged(tmp_path):
    from unittest.mock import MagicMock

    store = MagicMock()
    seen_head_updates: list[int] = []

    def walk(_driver, _version, _seed):
        seen_head_updates.append(store.update_repo_head_sha.call_count)
        return {"dep_mod": ("downstream_repo", 1)}

    fake_store, _walk, _parsed = _run_index_repo(tmp_path, walk=walk, store=store)

    assert seen_head_updates == [0], "head_sha advanced before the propagation ran"
    fake_store.update_repo_head_sha.assert_called_once()
    fake_store.reset_head_sha.assert_called_once_with([99])


def test_a_walk_error_falls_back_to_the_direct_reset_and_the_repo_is_not_failed(
    tmp_path, caplog,
):
    def stack_extra(stack):
        from unittest.mock import patch
        stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_repos", return_value=["downstream_repo"],
        ))

    with caplog.at_level(logging.WARNING):
        fake_store, _walk, _parsed = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"), stack_extra=stack_extra,
        )

    fake_store.reset_head_sha.assert_called_once_with([99])
    fake_store.update_repo_head_sha.assert_called_once()
    assert "dependency walk failed" in caplog.text


def test_a_propagation_that_cannot_be_recorded_keeps_head_sha_and_does_not_fail(
    tmp_path, caplog,
):
    """Walk and fallback both fail (Neo4j down after the writes): the repo is
    not reported failed, and head_sha stays so the next run repeats it."""
    def stack_extra(stack):
        from unittest.mock import patch
        stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_repos",
            side_effect=RuntimeError("Neo4j ServiceUnavailable"),
        ))

    with caplog.at_level(logging.WARNING):
        fake_store, _walk, _parsed = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"), stack_extra=stack_extra,
        )

    fake_store.update_repo_head_sha.assert_not_called()
    assert "could not be recorded" in caplog.text
    assert "head_sha is not advanced" in caplog.text


# ---------------------------------------------------------------------------
# The seed: only modules whose graph this run can have changed (finding 3)
# ---------------------------------------------------------------------------


def test_a_module_re_written_only_for_a_rewrite_reason_does_not_propagate(tmp_path):
    """mod_old is re-parsed for the shared-module bootstrap (its source did not
    change), mod_new is new in git: only mod_new seeds the propagation."""
    _store, walk, parsed = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, bootstrap=["mod_old"],
    )

    assert "mod_old" in parsed, "precondition: mod_old was re-written"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_new"}


def test_a_run_that_only_re_writes_unchanged_modules_propagates_nothing(tmp_path):
    """The ping-pong guard: a repo re-writing its dependents (flagged by another
    repo) or retrying their embeddings must not flag anything back."""
    store, walk, parsed = _run_index_repo(tmp_path, bootstrap=["mod_old"], meta_only=True)

    assert parsed == ["mod_old"], "precondition: mod_old was re-written"
    walk.assert_not_called()
    store.reset_head_sha.assert_not_called()
    store.update_repo_head_sha.assert_called_once()


@pytest.mark.parametrize(
    ("reason", "propagates"),
    [("no_node", True), ("path_drift", True), ("repo_drift", False)],
)
def test_self_heal_seeds_only_when_the_graph_can_change(reason, propagates):
    from src.indexer.pipeline_repo import propagation_seed

    seed = propagation_seed(
        set(), heal={"healed": {"reason": reason}}, degraded_changed=set(),
        gap_changed=set(), written={"healed"},
    )

    assert (seed == {"healed"}) is propagates


def test_the_seed_keeps_changed_sources_and_drops_what_was_not_written():
    from src.indexer.pipeline_repo import propagation_seed

    seed = propagation_seed(
        {"git_changed", "not_written"}, heal={}, degraded_changed={"files_fixed"},
        gap_changed={"gap_reevaluated"},
        written={"git_changed", "files_fixed", "gap_reevaluated", "rewrite_only"},
    )

    assert seed == {"git_changed", "files_fixed", "gap_reevaluated"}
