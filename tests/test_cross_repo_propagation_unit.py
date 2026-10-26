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
  reset is the fallback when the run has no ledger or the flag write fails,
  and it covers the repos of EVERY dependent, transitive ones included.
- The dependency walk ends on a dependency cycle.
- Every run mode propagates what it changed: the incremental diff, the sync
  path's one-shot seeds, a full run's changes since its baseline.
- A propagation that cannot be recorded in full keeps the repo's head_sha
  (the next run repeats it), is named in ``lifecycle_attention``, and is an
  alert only when it was not recorded in the previous run either.
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

    def flag_dependents(self, odoo_version, names, *, pending=(), exclude_repo_ids=()):
        """The combined store call: direct flags + pending entries, one hold."""
        self.calls = getattr(self, "calls", 0) + 1
        hit = self.mark_dependents_needs_rewrite(
            odoo_version, names, exclude_repo_ids=exclude_repo_ids,
        )
        self.pending = sorted(pending)
        return hit, len(self.pending)


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


def test_without_a_ledger_the_repos_of_every_dependent_are_reset():
    result, repos = _propagate({"to_git"}, ledger=None)

    # saas-infrastructure ships a DIRECT dependent; erponline only a transitive
    # one, which a one-hop reset never reaches (a ledger-less full re-index
    # does not propagate further).
    assert repos.reset == [32, 42]
    assert result.marked == {}


def test_a_failed_flag_write_resets_the_repos_of_every_dependent_transitive_included(caplog):
    """erponline-enterprise ships only a depth-2 dependent: a reset of the
    direct dependents' repos left it with stale edges while the propagation
    counted as recorded."""
    ledger = _Ledger(LEDGER_ROWS, fail=True)

    with caplog.at_level(logging.WARNING, logger="src.indexer.cross_repo"):
        result, repos = _propagate({"to_git"}, ledger=ledger)

    assert repos.reset == [32, 42]
    assert result.reset_repo_ids == [32, 42]
    assert result.marked == {}
    # A reset is lost when the dependent repo's run is in progress, and no
    # pending entry was written: retried by the next run.
    assert result.complete is False
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
    # The transitive dependents are unknown: the caller must repeat the walk.
    assert result.complete is False
    assert "dependency walk failed" in caplog.text


def test_a_failed_walk_and_fallback_report_an_incomplete_propagation():
    repos = _Repos(REPOS)

    result = propagate_to_dependents(
        _WalkFailsDriver(None), V, {"to_git"},
        exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
        presence=_Ledger(LEDGER_ROWS), repo_store=repos,
    )

    assert result.complete is False
    assert repos.reset == []


def _run_index_repo(
    tmp_path, *, walk=None, bootstrap=(), meta_only=False, stack_extra=None, store=None,
    attention=None, stored_head="a", full_reindex=False, ledger_store=False,
    presence_head=None,
):
    """``_index_repo`` on a real two-commit git repo (in-memory parsers, no DB).

    Commit A ships ``mod_old``; commit B adds ``mod_new`` (or, with
    *meta_only*, only touches a top-level file). ``repo_store`` reports
    head_sha A, so the run is incremental A..B. *walk* replaces
    ``cross_repo.find_dependent_modules`` (a callable side effect or a return
    value); *bootstrap* are the names the shared-module bootstrap re-parses.
    *store* (a MagicMock) stands for ``repo_store()``; pass one to inspect
    it after a raising run. *attention* is the repo row's
    ``lifecycle_attention`` from the previous run. *stored_head*: the
    ``repos.head_sha`` the store reports, ``"a"`` (commit A), ``"b"`` (HEAD:
    nothing moved) or None (reset). *ledger_store*: ``_presence_store``
    returns a fake ledger whose ``presence_head_sha`` is *presence_head*
    (same labels) while this repo's lifecycle is off (no ledger commit), so
    the attention is stored and read back like with a ledger. Returns
    ``(fake_store, walk_mock, parsed_module_names, counters)``.
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
        "lifecycle_attention": attention,
    }
    fake_store = store if store is not None else MagicMock()
    fake_store.get_repo_head_sha.return_value = {
        "a": sha_a, "b": _head(repo), None: None,
    }[stored_head]
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
        if ledger_store:
            _install_ledger_store_without_lifecycle(
                stack, {"a": sha_a, "b": _head(repo), None: None}[presence_head],
            )
        if stack_extra is not None:
            stack_extra(stack)
        counters = _index_repo(
            repo_row, MagicMock(), pg_conn=object(), refresh=False,
            full_reindex=full_reindex,
        )
    return fake_store, walk_mock, parsed, counters


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

    fake_store, _walk, _parsed, _c = _run_index_repo(tmp_path, walk=walk, store=store)

    assert seen_head_updates == [0], "head_sha advanced before the propagation ran"
    fake_store.update_repo_head_sha.assert_called_once()
    fake_store.reset_head_sha.assert_called_once_with([99])


def test_a_walk_error_resets_the_direct_dependents_and_keeps_head_sha(
    tmp_path, caplog,
):
    """The direct dependents' repos are reset at once, but the walk is what
    finds the transitive ones: head_sha is kept so the next run repeats it,
    and the repo is not reported failed."""
    def stack_extra(stack):
        from unittest.mock import patch
        stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_repos", return_value=["downstream_repo"],
        ))

    with caplog.at_level(logging.WARNING):
        fake_store, _walk, _parsed, counters = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"), stack_extra=stack_extra,
        )

    fake_store.reset_head_sha.assert_called_once_with([99])
    fake_store.update_repo_head_sha.assert_not_called()
    assert "dependency walk failed" in caplog.text
    assert counters["propagation"]["unrecorded"]


def test_a_propagation_that_cannot_be_recorded_keeps_head_sha_and_does_not_fail(
    tmp_path, caplog,
):
    """Walk and fallback both fail (Neo4j down after the writes), or a reset
    write raises: the repo is not reported failed, and head_sha stays so the
    next run repeats it."""
    def stack_extra(stack):
        from unittest.mock import patch
        stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_repos",
            side_effect=RuntimeError("Neo4j ServiceUnavailable"),
        ))

    with caplog.at_level(logging.WARNING):
        fake_store, _walk, _parsed, _c = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"), stack_extra=stack_extra,
        )

    fake_store.update_repo_head_sha.assert_not_called()
    assert "head_sha is not advanced" in caplog.text


def test_a_failed_reset_write_keeps_head_sha_and_does_not_fail(tmp_path, caplog):
    from unittest.mock import MagicMock

    store = MagicMock()
    store.reset_head_sha.side_effect = RuntimeError("PostgreSQL connection reset")
    with caplog.at_level(logging.WARNING):
        _store, _walk, _parsed, counters = _run_index_repo(
            tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, store=store,
        )

    store.update_repo_head_sha.assert_not_called()
    assert "could not be recorded" in caplog.text
    assert counters["propagation"]["unrecorded"]


# ---------------------------------------------------------------------------
# The seed: only modules whose graph this run can have changed (finding 3)
# ---------------------------------------------------------------------------


def test_a_module_re_written_only_for_a_rewrite_reason_does_not_propagate(tmp_path):
    """mod_old is re-parsed for the shared-module bootstrap (its source did not
    change), mod_new is new in git: only mod_new seeds the propagation."""
    _store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, bootstrap=["mod_old"],
    )

    assert "mod_old" in parsed, "precondition: mod_old was re-written"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_new"}


def test_a_run_that_only_re_writes_unchanged_modules_propagates_nothing(tmp_path):
    """The ping-pong guard: a repo re-writing its dependents (flagged by another
    repo) or retrying their embeddings must not flag anything back."""
    store, walk, parsed, _c = _run_index_repo(tmp_path, bootstrap=["mod_old"], meta_only=True)

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


# ---------------------------------------------------------------------------
# Every run mode propagates what it changed (round 2, item 2)
# ---------------------------------------------------------------------------


def test_the_sync_path_propagates_its_one_shot_seeds(tmp_path):
    """HEAD did not move, but a degraded module's failing files changed on
    disk: the sync path re-parses it, and its dependents must hear of it (the
    sync path used to never propagate, so the one-shot seed was lost)."""
    def stack_extra(stack):
        from unittest.mock import patch

        import src.indexer.pipeline_repo as _pr
        stack.enter_context(patch.object(_pr, "_degraded_state", return_value=({}, {"mod_old"})))

    _store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, stored_head="b",
        stack_extra=stack_extra,
    )

    assert parsed == ["mod_old"], "precondition: the sync path re-wrote only mod_old"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_old"}


def test_a_full_run_propagates_the_modules_changed_since_its_baseline(tmp_path):
    """``--full`` re-writes every module; only mod_new changed since the
    stored head_sha, so only mod_new seeds (full runs used to never propagate)."""
    _store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, full_reindex=True,
    )

    assert sorted(parsed) == ["mod_new", "mod_old"], "precondition: a full run"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_new"}


def test_a_full_run_without_baseline_or_ledger_does_not_propagate(tmp_path):
    """head_sha reset and no ledger: a first index cannot be told from a reset,
    and seeding every module after each reset would bounce full re-indexes
    between ledger-less repos that depend on each other."""
    store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, stored_head=None,
    )

    assert sorted(parsed) == ["mod_new", "mod_old"]
    walk.assert_not_called()
    store.update_repo_head_sha.assert_called_once()


@pytest.mark.parametrize(
    ("mode", "since", "ledger", "expected"),
    [
        ("incremental", None, True, {"changed"}),
        ("sync", None, True, set()),
        ("full", {"moved"}, True, {"moved"}),
        ("full", set(), True, set()),
        ("full", None, True, {"changed"}),
        ("full", None, False, set()),
    ],
    ids=["incremental", "sync", "full-diff", "full-no-change", "full-first-index",
         "full-no-baseline-no-ledger"],
)
def test_source_changed_names_per_run_mode(mode, since, ledger, expected):
    from src.indexer import pipeline_repo as pr

    mode_value = {"incremental": pr.RUN_INCREMENTAL, "sync": pr.RUN_SYNC, "full": pr.RUN_FULL}
    got = pr.source_changed_names(mode_value[mode], {"changed"}, ledger=ledger, since=since)

    assert got == expected


def test_a_full_run_baseline_falls_back_to_the_ledger_head_and_survives_a_force_push(
    tmp_path,
):
    """head_sha NULL (reset) leaves the ledger's presence head as the baseline;
    a force-pushed old head still in the object store is diffed too; a head
    the checkout does not have is skipped."""
    from src.indexer.pipeline_repo import full_run_changed_names
    from src.indexer.registry import build_registry_scan
    from tests.test_pipeline_refresh import _add_module, _git, _head, _make_repo

    repo = _make_repo(tmp_path / "addons", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    _add_module(repo, "mod_gone")
    sha_b = _head(repo)
    _git(repo, "reset", "--hard", sha_a)  # force-push: B is no longer an ancestor
    _add_module(repo, "mod_new")
    head = _head(repo)
    scan = build_registry_scan(str(repo), "17.0", branch="17.0")

    assert full_run_changed_names(repo, scan, head, [None, sha_a]) == {"mod_new"}
    assert full_run_changed_names(repo, scan, head, [sha_b, None]) == {"mod_new"}
    assert full_run_changed_names(repo, scan, head, ["0" * 40, sha_a]) == {"mod_new"}
    assert full_run_changed_names(repo, scan, head, [head, sha_a]) == set()
    assert full_run_changed_names(repo, scan, head, [None, "0" * 40]) is None


# ---------------------------------------------------------------------------
# An unrecorded propagation is visible (round 2, item 4)
# ---------------------------------------------------------------------------


def _walk_and_fallback_fail(stack):
    from unittest.mock import patch
    stack.enter_context(patch(
        "src.indexer.cross_repo.find_dependent_repos",
        side_effect=RuntimeError("Neo4j ServiceUnavailable"),
    ))


def test_an_unrecorded_propagation_is_named_with_the_kept_head(tmp_path, caplog):
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    with caplog.at_level(logging.WARNING):
        store, _walk, _parsed, counters = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"),
            stack_extra=_walk_and_fallback_fail,
        )

    stuck = store.get_repo_head_sha.return_value[:12]
    line = counters["propagation"]["unrecorded"]
    assert PROPAGATION_UNRECORDED_MARK in line and stuck in line and "mod_new" in line
    assert counters["propagation"]["persistent"] is False
    # Without a ledger the attention lines are logged (WARNING) instead of stored.
    assert any(PROPAGATION_UNRECORDED_MARK in r.getMessage() for r in caplog.records)


def test_the_second_unrecorded_run_in_a_row_is_persistent(tmp_path):
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    _store, _walk, _parsed, counters = _run_index_repo(
        tmp_path, walk=RuntimeError("Neo4j TransientError"),
        stack_extra=_walk_and_fallback_fail,
        attention=f"{PROPAGATION_UNRECORDED_MARK} for 1 module(s) (mod_new) at HEAD x",
    )

    assert counters["propagation"]["persistent"] is True
    assert "two consecutive runs" in counters["propagation"]["unrecorded"]


def test_once_is_a_warning_twice_in_a_row_is_an_alert(capsys):
    """One unrecorded propagation retries by itself (head_sha kept): printed,
    exit 0. The same repo again in the next run is exit 3 (OnFailure= mail)."""
    from src.indexer import pipeline as pipeline_mod
    from src.indexer.__main__ import EXIT_LIFECYCLE_ATTENTION, _lifecycle_exit_code

    def summary(persistent: bool) -> dict:
        lc = pipeline_mod._empty_lifecycle()
        pipeline_mod._absorb_repo_lifecycle(lc, {"id": 7, "url": "file://up"}, {
            "propagation": {"unrecorded": "cross-repo propagation not recorded ...",
                            "persistent": persistent},
        })
        return pipeline_mod._finish_lifecycle(lc)

    once = summary(False)
    assert once["propagation_unrecorded"] and once["stale_propagation"] == []
    assert once["needs_attention"] is False
    assert _lifecycle_exit_code(once) == 0
    assert "Cross-repo propagation not recorded" in capsys.readouterr().err

    twice = summary(True)
    assert twice["needs_attention"] is True
    assert _lifecycle_exit_code(twice) == EXIT_LIFECYCLE_ATTENTION
    err = capsys.readouterr().err
    assert "stale_propagation: repo id=7" in err
    # Printed once, and never as "retried by the next run": it was not.
    assert err.count("repo id=7 (file://up)") == 1
    assert "retried by the next run" not in err


# ---------------------------------------------------------------------------
# An unrecorded propagation is retried in every run mode (round 3, MAJOR 1)
# ---------------------------------------------------------------------------


def _degraded(name):
    def apply(stack):
        from unittest.mock import patch

        import src.indexer.pipeline_repo as _pr
        stack.enter_context(patch.object(_pr, "_degraded_state", return_value=({}, {name})))
    return apply


class _StatefulLedger:
    """A fake ``module_presence`` store that remembers, between runs, what the
    retry depends on: the presence head and the repo's ``lifecycle_attention``."""

    def __init__(self, presence_head: str | None) -> None:
        from unittest.mock import MagicMock

        self.presence_head = presence_head
        self.attention: str | None = None
        self.store = MagicMock()
        self.store.presence_head_sha.side_effect = lambda _rid: self.presence_head
        self.store.needs_rewrite_names.return_value = []
        self.store.needs_rewrite_snapshot.return_value = ([], None)
        self.store.apply_pending_rewrites.return_value = []
        self.store.mark_dependents_needs_rewrite.return_value = []
        self.store.flag_dependents.return_value = ([], 0)
        self.store.repo_ids_without_rows.return_value = []
        self.store.present_owner_basenames.return_value = {}
        self.store.mark_retire_pending.return_value = []
        self.store.mark_presence_synced.side_effect = self._synced
        self.store.set_lifecycle_attention.side_effect = self._attend
        self.store.clear_lifecycle_attention.side_effect = self._clear

    def _synced(self, _rid, head, **_kw):
        self.presence_head = head

    def _attend(self, _rid, text, **_kw):
        self.attention = text

    def _clear(self, _rid, **_kw):
        self.attention = None


class _LifecycleRepo:
    """One real git repo indexed several times by ``_index_repo`` with the
    lifecycle ON over a :class:`_StatefulLedger` (no DB): the ledger commit,
    the presence head and the attention carry from one run to the next."""

    def __init__(self, tmp_path, *, meta_only: bool = False) -> None:
        from tests.test_pipeline_refresh import _add_module, _git, _head, _make_repo

        self.repo = _make_repo(tmp_path / "upstream_repo", branch="17.0")
        _add_module(self.repo, "mod_old")
        self.sha_a = _head(self.repo)
        if meta_only:
            (self.repo / "README.md").write_text("docs only\n")
            _git(self.repo, "add", "README.md")
            _git(self.repo, "commit", "-m", "docs")
        else:
            _add_module(self.repo, "mod_new")
        self.sha_b = _head(self.repo)

    def sha(self, label):
        return {"a": self.sha_a, "b": self.sha_b, None: None}[label]

    def run(self, ledger: _StatefulLedger, *, head_sha, walk, extra=(), commit_error=None):
        """One run; returns ``(store, walk_mock, counters)``. *head_sha* is the
        ``repos.head_sha`` label at the start; *walk* as in ``_run_index_repo``."""
        import contextlib
        from dataclasses import replace
        from unittest.mock import MagicMock, patch

        import src.indexer.pipeline as _pipeline
        import src.indexer.pipeline_repo as _pr
        from src.indexer.pipeline import _index_repo
        from tests.test_pipeline_refresh import _install_inmemory_parsers

        store = MagicMock()
        store.get_repo_head_sha.return_value = self.sha(head_sha)
        store.get_repo_ids_by_local_path_basenames.return_value = [99]
        store.reset_head_sha.return_value = 1
        writer = MagicMock()
        writer.stamp_module_presence.return_value = 10**6
        repo_row = {
            "id": 7, "local_path": str(self.repo), "odoo_version": "17.0",
            "url": "file://upstream", "branch": "17.0", "ssh_key_id": None,
            "lifecycle_attention": ledger.attention,
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(_pipeline, "repo_store", return_value=store))
            walk_kw = (
                {"side_effect": walk} if callable(walk) or isinstance(walk, BaseException)
                else {"return_value": walk}
            )
            walk_mock = stack.enter_context(
                patch("src.indexer.cross_repo.find_dependent_modules", **walk_kw)
            )
            _install_inmemory_parsers(stack, {})
            stack.enter_context(
                patch.object(_pipeline, "_presence_store", return_value=ledger.store)
            )
            stack.enter_context(patch.object(_pr, "shared_parse_bootstrap", return_value=[]))
            stack.enter_context(patch.object(_pr, "self_heal_rewrites", return_value={}))
            real_observe = _pr.observe_lifecycle

            def lifecycle_on(repo, scan, presence, current_head, **kw):
                return replace(real_observe(repo, scan, None, current_head, **kw),
                               lifecycle_on=True)

            stack.enter_context(
                patch.object(_pr, "observe_lifecycle", side_effect=lifecycle_on)
            )
            if commit_error is not None:
                stack.enter_context(
                    patch.object(_pr, "_commit_lifecycle", side_effect=commit_error)
                )
            for e in extra:
                e(stack)
            counters = _index_repo(repo_row, writer, pg_conn=object(), refresh=False)
        return store, walk_mock, counters


_FAIL = RuntimeError("Neo4j TransientError")
_DEPS = {"dep_mod": ("downstream_repo", 1)}

# (presence head at start, repos.head_sha label, meta_only, run-1 extras, retry seed)
_MODES = {
    # A full run after a head_sha reset, diffed against the presence head A:
    # the held presence head reproduces the git seed, nothing to carry.
    "full": ("a", None, False, (), {"mod_new"}),
    # The sync path at an unchanged HEAD with a degraded one-shot seed: run 2
    # would skip; the mark carries mod_old.
    "sync": ("b", "b", False, (_degraded("mod_old"),), {"mod_old"}),
    # An incremental run whose only seed was one-shot (the diff touched no
    # module): run 2 repeats the diff, the mark carries mod_old.
    "incremental-one-shot": ("a", "a", True, (_degraded("mod_old"),), {"mod_old"}),
}


def _first_failed_run(tmp_path, mode):
    presence, head_sha, meta_only, extra, _expected = _MODES[mode]
    repo = _LifecycleRepo(tmp_path, meta_only=meta_only)
    ledger = _StatefulLedger(repo.sha(presence))
    store, _walk, counters = repo.run(
        ledger, head_sha=head_sha, walk=_FAIL, extra=(_walk_and_fallback_fail, *extra),
    )
    return repo, ledger, store, counters


@pytest.mark.parametrize("mode", list(_MODES))
def test_an_unrecorded_propagation_holds_the_ledger_and_the_head(tmp_path, mode):
    """The ``_index_repo`` wiring of the hold: the presence head neither
    advances nor is deferred to the reconcile, head_sha is kept, and the
    mark is in the stored attention."""
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    repo, ledger, store, counters = _first_failed_run(tmp_path, mode)

    ledger.store.commit_observed.assert_called_once()  # the ledger commit ran
    ledger.store.mark_presence_synced.assert_not_called()
    assert counters["lifecycle"]["presence_deferred_head"] is None
    assert counters["lifecycle"]["presence_held_head"] == repo.sha_b
    store.update_repo_head_sha.assert_not_called()
    assert PROPAGATION_UNRECORDED_MARK in (ledger.attention or "")


@pytest.mark.parametrize("mode", list(_MODES))
def test_the_next_run_retries_an_unrecorded_propagation(tmp_path, mode):
    """Run 2 starts from what run 1 left in the ledger (presence head, stored
    attention) and seeds what run 1 failed to propagate - exactly, not every
    present module."""
    repo, ledger, _store, _c = _first_failed_run(tmp_path, mode)
    _presence, head_sha, _meta, _extra, expected = _MODES[mode]

    _store2, walk, counters = repo.run(ledger, head_sha=head_sha, walk=_DEPS)

    walk.assert_called_once()
    assert walk.call_args.args[2] == expected
    assert counters["propagation"] == {}, "recorded this time"
    assert ledger.presence_head == repo.sha_b, "recorded: the presence head advances"
    assert ledger.attention is None or "not recorded" not in ledger.attention


@pytest.mark.parametrize("mode", list(_MODES))
def test_a_second_unrecorded_run_is_exit_3_in_every_mode(tmp_path, mode):
    from src.indexer import pipeline as pipeline_mod
    from src.indexer.__main__ import EXIT_LIFECYCLE_ATTENTION, _lifecycle_exit_code

    repo, ledger, _store, _c = _first_failed_run(tmp_path, mode)
    _presence, head_sha, _meta, _extra, _expected = _MODES[mode]

    store2, _walk, counters = repo.run(
        ledger, head_sha=head_sha, walk=_FAIL, extra=(_walk_and_fallback_fail,),
    )

    assert counters["propagation"]["persistent"] is True
    store2.update_repo_head_sha.assert_not_called()
    lc = pipeline_mod._empty_lifecycle()
    pipeline_mod._absorb_repo_lifecycle(lc, {"id": 7, "url": "file://upstream"}, counters)
    assert _lifecycle_exit_code(pipeline_mod._finish_lifecycle(lc)) == EXIT_LIFECYCLE_ATTENTION


def test_the_retry_mark_survives_a_failing_ledger_commit(tmp_path):
    """The mark is stored right after the propagation failed: a ledger commit
    that raises afterwards (or a stop) must not lose the retry."""
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    repo = _LifecycleRepo(tmp_path)
    ledger = _StatefulLedger(repo.sha("a"))
    with pytest.raises(RuntimeError, match="ledger lock"):
        repo.run(
            ledger, head_sha="a", walk=_FAIL, extra=(_walk_and_fallback_fail,),
            commit_error=RuntimeError("ledger lock retire:17.0 not acquired"),
        )

    assert PROPAGATION_UNRECORDED_MARK in (ledger.attention or "")


def test_a_retry_mark_forces_a_run_at_an_unchanged_head():
    from pathlib import Path

    from src.indexer import pipeline_repo as pr

    skip = pr.plan_repo_run(Path("/x"), "h", "h", "h", [], full_reindex=False, ledger=True)
    retry = pr.plan_repo_run(
        Path("/x"), "h", "h", "h", [], full_reindex=False, ledger=True,
        retry_propagation=True,
    )

    assert skip.mode == pr.RUN_SKIP
    assert retry.mode == pr.RUN_SYNC


def test_the_retry_mark_needs_a_ledger_and_the_previous_runs_line():
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK, propagation_retry

    marked = {"lifecycle_attention": f"x; {PROPAGATION_UNRECORDED_MARK} for 2 module(s)"}
    assert propagation_retry(marked, ledger=True) is not None
    assert propagation_retry(marked, ledger=False) is None
    assert propagation_retry({"lifecycle_attention": "pre-scan fetch failed"}, ledger=True) is None
    assert propagation_retry({}, ledger=True) is None


def _commit(tmp_path, *, hold_presence: bool, pending: bool = False):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.indexer.pipeline_repo import _commit_lifecycle
    from src.indexer.registry import build_registry_scan
    from tests.test_pipeline_refresh import _add_module, _head, _make_repo

    repo = _make_repo(tmp_path / "addons", branch="17.0")
    _add_module(repo, "mod_a")
    head = _head(repo)
    scan = build_registry_scan(str(repo), "17.0", branch="17.0")
    presence = MagicMock()
    presence.present_owner_basenames.return_value = {}
    presence.mark_retire_pending.return_value = ["gone_mod"] if pending else []
    writer = MagicMock()
    writer.stamp_module_presence.return_value = len(scan.present_names())
    gates = SimpleNamespace(kept_names=set(), retire_allowed=True, tripped=[])
    transitions = SimpleNamespace(
        retired=(SimpleNamespace(name="gone_mod", path="gone_mod"),) if pending else (),
    )
    counters = _commit_lifecycle(
        presence, writer, {"id": 7, "local_path": str(repo), "url": "file://addons"},
        scan=scan, rows=[], transitions=transitions, gates=gates, current_head=head,
        diff_base=None, owning_profile="p", retire=True, written=set(), rewrite_names=[],
        attention=[], hold_presence=hold_presence,
    )
    return presence, counters


def test_an_unrecorded_propagation_holds_the_ledger_presence_head(tmp_path):
    """The presence head is the "index reflects HEAD" claim the next run's skip
    and a full run's baseline read: it must not advance (nor be handed to the
    reconcile) while other repos' dependents were not flagged."""
    presence, counters = _commit(tmp_path, hold_presence=True)

    presence.mark_presence_synced.assert_not_called()
    assert counters["presence_synced"] is False
    assert counters["presence_deferred_head"] is None
    presence.commit_observed.assert_called_once()  # the rows themselves are committed


def test_a_recorded_propagation_still_syncs_the_ledger(tmp_path):
    presence, counters = _commit(tmp_path, hold_presence=False)

    presence.mark_presence_synced.assert_called_once()
    assert counters["presence_synced"] is True


def test_a_repo_whose_lifecycle_is_off_never_seeds_every_module_on_a_reset(tmp_path):
    """MINOR 1: a ledger store exists but this repo's lifecycle is off (no git
    HEAD / branch), so it never gets a presence head: a reset must not seed
    every module, or two such repos depending on each other bounce."""
    store, walk, _parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, stored_head=None,
        ledger_store=True,
    )

    walk.assert_not_called()
    store.update_repo_head_sha.assert_called_once()


def _install_ledger_store_without_lifecycle(stack, presence_head):
    """``_presence_store`` returns a store, but observe_lifecycle reports the
    lifecycle off for this repo (as for a checkout without a branch)."""
    from dataclasses import replace
    from unittest.mock import MagicMock, patch

    import src.indexer.pipeline as _pipeline
    import src.indexer.pipeline_repo as _pr

    fake = MagicMock()
    fake.presence_head_sha.return_value = presence_head
    fake.needs_rewrite_names.return_value = []
    fake.needs_rewrite_snapshot.return_value = ([], None)
    fake.mark_dependents_needs_rewrite.return_value = []
    fake.flag_dependents.return_value = ([], 0)
    fake.repo_ids_without_rows.return_value = []
    stack.enter_context(patch.object(_pipeline, "_presence_store", return_value=fake))
    real = _pr.observe_lifecycle

    def off(repo, scan, presence, current_head, **kw):
        return replace(real(repo, scan, None, current_head, **kw), lifecycle_on=False)

    stack.enter_context(patch.object(_pr, "observe_lifecycle", side_effect=off))


# ---------------------------------------------------------------------------
# The retry mark: what it carries, and who must not drop it (round 4)
# ---------------------------------------------------------------------------


def test_the_mark_carries_the_one_shot_names_and_the_retry_reads_them():
    from src.indexer.pipeline_repo import _propagation_attention, propagation_retry

    line, _ = _propagation_attention(
        {}, {"mod_new", "mod_gap"}, kept_head="a" * 40, head="b" * 40, one_shot={"mod_gap"},
    )
    retry = propagation_retry({"lifecycle_attention": f"other; {line}"}, ledger=True)

    assert "[reseed: mod_gap]" in line
    assert retry.reseed == {"mod_gap"} and retry.reseed_all is False
    assert retry.failed_at == "b" * 12


def test_a_git_only_seed_carries_no_names():
    """Incremental / full: the kept head_sha and the held presence head
    reproduce the git seed, so nothing is carried and nothing extra re-seeded."""
    from src.indexer.pipeline_repo import _propagation_attention, propagation_retry

    line, _ = _propagation_attention({}, {"mod_new"}, kept_head="a", head="b", one_shot=set())
    retry = propagation_retry({"lifecycle_attention": line}, ledger=True)

    assert "[reseed:" not in line
    assert retry.reseed == frozenset() and retry.reseed_all is False


def test_too_many_one_shot_names_fall_back_to_re_seeding_every_module():
    from src.indexer.pipeline_repo import (
        PROPAGATION_RESEED_NAMES_MAX,
        _propagation_attention,
        propagation_retry,
    )

    names = {f"m{i}" for i in range(PROPAGATION_RESEED_NAMES_MAX + 1)}
    line, _ = _propagation_attention({}, names, kept_head="a", head="b", one_shot=names)

    assert line.endswith("[reseed: all]")
    assert propagation_retry({"lifecycle_attention": line}, ledger=True).reseed_all is True


def test_a_failed_reseed_all_retry_names_the_original_failure_not_the_inflated_seed():
    from src.indexer.pipeline_repo import (
        PropagationRetry,
        _propagation_attention,
        propagation_retry,
    )

    previous = PropagationRetry(frozenset(), True, "c0ffee123456")
    line, consecutive = _propagation_attention(
        {"lifecycle_attention": "cross-repo propagation not recorded ..."},
        {f"m{i}" for i in range(300)}, kept_head="a", head="d" * 40,
        one_shot={f"m{i}" for i in range(300)}, retry=previous,
    )

    assert consecutive is True
    assert "300 module(s)" not in line
    assert "re-seeded after an unrecorded propagation at HEAD c0ffee123456" in line
    again = propagation_retry({"lifecycle_attention": line}, ledger=True)
    assert again.reseed_all is True and again.failed_at == "c0ffee123456"


def test_writers_that_replace_the_attention_keep_the_retry_mark():
    """An unavailable checkout (pipeline._report_unavailable_repo) replaces the
    attention; the mark must survive until a run records the propagation."""
    from unittest.mock import MagicMock, patch

    from src.indexer import pipeline as pipeline_mod
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK, RepoCheckoutUnavailable

    mark = f"{PROPAGATION_UNRECORDED_MARK} for 1 module(s) (m) at HEAD b: ... [reseed: m]"
    repo = {"id": 7, "url": "file://up", "local_path": "/gone",
            "lifecycle_attention": f"pre-scan fetch failed (x); {mark}"}
    ledger = MagicMock()
    with (
        patch.object(pipeline_mod, "_presence_store", return_value=ledger),
        patch.object(pipeline_mod, "repo_store", return_value=MagicMock()),
    ):
        pipeline_mod._report_unavailable_repo(
            pipeline_mod._empty_lifecycle(), repo,
            RepoCheckoutUnavailable(repo, "clone failed: auth"),
        )

    (_rid, text), _kw = ledger.set_lifecycle_attention.call_args
    assert text.startswith("checkout unavailable: clone failed: auth")
    assert mark in text
    assert "pre-scan fetch failed" not in text


def test_carry_propagation_mark_adds_only_mark_segments_once():
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK, carry_propagation_mark

    mark = f"{PROPAGATION_UNRECORDED_MARK} for 1 module(s) (m)"
    assert carry_propagation_mark(f"a; {mark}; b", ["c"]) == ["c", mark]
    assert carry_propagation_mark(mark, [mark]) == [mark]
    assert carry_propagation_mark(None, ["c"]) == ["c"]


def test_the_audit_shows_a_pending_retry_and_its_weight():
    from src.indexer.lifecycle_audit import _new_repo_entry, propagation_retry_entry
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    base = {"id": 1, "url": "u", "local_path": "/x", "branch": "17.0", "odoo_version": "17.0",
            "profile_name": "p"}
    assert _new_repo_entry(base)["propagation_retry"] == {
        "pending": False, "reseed_all": False, "reseed": [],
    }
    assert propagation_retry_entry(base) == {"pending": False, "reseed_all": False, "reseed": []}
    one = {**base, "lifecycle_attention": f"{PROPAGATION_UNRECORDED_MARK} ... [reseed: b,a]"}
    assert propagation_retry_entry(one) == {"pending": True, "reseed_all": False,
                                            "reseed": ["a", "b"]}
    heavy = {**base, "lifecycle_attention": f"{PROPAGATION_UNRECORDED_MARK} ... [reseed: all]"}
    assert propagation_retry_entry(heavy)["reseed_all"] is True


def test_a_retired_name_is_stamped_with_the_head_the_run_observed():
    """state_changed_sha is "the sha at which the state was entered" (the
    ledger). A run that kept head_sha back at X for an unrecorded propagation
    saw the name gone at Y: the reconcile records Y, not X (which still
    shipped it)."""
    from unittest.mock import MagicMock

    from src.indexer.reconcile import ReconcileReport, _Reconciler

    store = MagicMock()
    store.repo_sync_state.return_value = [{"repo_id": 7, "head_sha": "x" * 40}]
    rec = _Reconciler(
        "17.0", writer=MagicMock(), store=store, conn=None, run_started_at=None,
        retire=True, allow_mass_retire=False, dry_run=False,
        report=ReconcileReport(odoo_version="17.0", dry_run=False, retire_enabled=True),
    )
    rec.observed_heads = {7: "y" * 40}

    rec._commit_retired([{"id": 1, "repo_id": 7, "name": "gone_mod"}])

    assert store.commit_retired.call_args.kwargs["head_sha"] == "y" * 40
    rec.observed_heads = {}
    rec._commit_retired([{"id": 2, "repo_id": 7, "name": "gone_mod2"}])
    assert store.commit_retired.call_args.kwargs["head_sha"] == "x" * 40


def test_the_held_head_reaches_the_reconcile():
    from unittest.mock import MagicMock, patch

    from src.indexer import pipeline as pipeline_mod

    lc = pipeline_mod._empty_lifecycle()
    pipeline_mod._absorb_repo_lifecycle(lc, {"id": 7, "url": "u"}, {
        "lifecycle": {"odoo_version": "17.0", "presence_held_head": "y" * 40},
    })
    seen = {}

    def fake_reconcile(version, **kw):
        seen[version] = kw["observed_heads"]
        return MagicMock(as_dict=dict, gates_tripped=[], undecidable=set(), errors={},
                         odoo_version=version)

    with (
        patch.object(pipeline_mod, "_presence_store", return_value=MagicMock()),
        patch("src.indexer.reconcile.reconcile_version", side_effect=fake_reconcile),
    ):
        pipeline_mod.run_lifecycle_reconcile(
            MagicMock(), {"17.0"}, run_started_at=None, lifecycle=lc,
        )

    assert seen == {"17.0": {7: "y" * 40}}


# ---------------------------------------------------------------------------
# A dependent without a ledger row yet (migration 0007, round 5 B)
# ---------------------------------------------------------------------------


def test_every_dependent_of_another_repo_is_offered_as_a_pending_flag():
    """The store keeps only the pairs whose repo has no present row of the
    name (a module written to the graph before its ledger commit); the
    propagation offers every dependent of another repo, never its own."""
    ledger = _Ledger(LEDGER_ROWS)

    result, _repos = _propagate({"to_git"}, ledger=ledger)

    assert ledger.pending == [
        ("erponline-enterprise", "viin_saas_portal"),
        ("saas-infrastructure", "to_odoo_saas"),
        ("saas-infrastructure", "to_server_ssh_git"),
    ]
    assert result.pending == 3 and result.complete is True


def test_the_direct_flags_and_the_pending_entries_are_one_store_call():
    """One transaction and one ledger lock hold (flag_dependents): two calls
    would let the dependent repo's ledger commit slip in between, turning a
    module the first call could not flag (no row) into one the second skips
    (a row now exists)."""
    ledger = _Ledger(LEDGER_ROWS)

    _propagate({"to_git"}, ledger=ledger)

    assert ledger.calls == 1
