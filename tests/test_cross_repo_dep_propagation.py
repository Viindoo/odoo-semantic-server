"""Cross-repo dependency change propagation tests (M7 W14).

Tests cover:
- find_dependent_repos returns Neo4j repo identifiers of modules that
  DEPENDS_ON changed modules (excluding the source repo itself).
- reset_head_sha bulk-NULLs head_sha for a list of repo IDs.
- get_repo_ids_by_local_path_basenames maps basename strings to repo IDs.
- End-to-end: incremental run on repo A resets head_sha of repo B when
  B's module DEPENDS_ON a changed module from A.
- Repos without dependency edges are untouched.
- The repo being indexed does not reset its own head_sha.

Business intent: if module `base` in repo A changes its API signature,
the next indexer run on repo B (which depends on `base`) re-indexes B
automatically — without admin intervention. Repos that don't depend on
the changed module are untouched.
"""
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.neo4j

TEST_VERSION = "99.0"


# ---------------------------------------------------------------------------
# Helpers — Neo4j
# ---------------------------------------------------------------------------

def _create_module_with_dep(driver, module_name: str, repo: str, depends_on: str | None) -> None:
    """Seed a Module node and optionally a DEPENDS_ON edge to another module."""
    with driver.session() as session:
        session.run(
            """
            MERGE (m:Module {name: $name, odoo_version: $v})
            SET m.repo = $repo
            """,
            name=module_name, v=TEST_VERSION, repo=repo,
        )
        if depends_on:
            session.run(
                """
                MATCH (m:Module {name: $name, odoo_version: $v})
                MERGE (d:Module {name: $dep, odoo_version: $v})
                MERGE (m)-[:DEPENDS_ON]->(d)
                """,
                name=module_name, v=TEST_VERSION, dep=depends_on,
            )


def _get_repo_value(driver, module_name: str) -> str | None:
    """Return the m.repo value for a module node (or None if absent)."""
    with driver.session() as session:
        row = session.run(
            "MATCH (m:Module {name: $name, odoo_version: $v}) RETURN m.repo AS repo",
            name=module_name, v=TEST_VERSION,
        ).single()
    return row["repo"] if row else None


# ---------------------------------------------------------------------------
# Tests for find_dependent_repos (Neo4j query only)
# ---------------------------------------------------------------------------

class TestFindDependentRepos:
    """Unit tests for cross_repo.find_dependent_repos."""

    def test_returns_dependent_repo(self, clean_neo4j):
        """Repo B's module depends on base from repo A → B is returned."""
        from src.indexer.cross_repo import find_dependent_repos

        driver = clean_neo4j

        # Repo A provides 'base'
        _create_module_with_dep(driver, "base", repo="repo_a", depends_on=None)
        # Repo B provides 'mymod' which depends on 'base'
        _create_module_with_dep(driver, "mymod", repo="repo_b", depends_on="base")

        result = find_dependent_repos(driver, TEST_VERSION, {"base"})
        assert "repo_b" in result, (
            f"Expected repo_b in result; got: {result}"
        )

    def test_excludes_repo_owning_changed_module(self, clean_neo4j):
        """Repo A owns 'base' (the changed module) — must NOT appear in result."""
        from src.indexer.cross_repo import find_dependent_repos

        driver = clean_neo4j

        _create_module_with_dep(driver, "base", repo="repo_a", depends_on=None)
        _create_module_with_dep(driver, "mymod", repo="repo_b", depends_on="base")

        result = find_dependent_repos(driver, TEST_VERSION, {"base"})
        assert "repo_a" not in result, (
            f"repo_a owns a changed module — must be excluded from result; got: {result}"
        )

    def test_skips_unrelated_repo(self, clean_neo4j):
        """Repo C's module has no DEPENDS_ON to base → not returned."""
        from src.indexer.cross_repo import find_dependent_repos

        driver = clean_neo4j

        _create_module_with_dep(driver, "base", repo="repo_a", depends_on=None)
        _create_module_with_dep(driver, "unrelated", repo="repo_c", depends_on=None)

        result = find_dependent_repos(driver, TEST_VERSION, {"base"})
        assert "repo_c" not in result, (
            f"repo_c has no DEPENDS_ON to base — must not appear; got: {result}"
        )

    def test_empty_changed_names_returns_empty(self, clean_neo4j):
        """Empty changed_module_names → empty result (no query needed)."""
        from src.indexer.cross_repo import find_dependent_repos

        driver = clean_neo4j
        result = find_dependent_repos(driver, TEST_VERSION, set())
        assert result == []

    def test_multiple_dependents(self, clean_neo4j):
        """Multiple repos depending on same changed module → all returned."""
        from src.indexer.cross_repo import find_dependent_repos

        driver = clean_neo4j

        _create_module_with_dep(driver, "base", repo="repo_a", depends_on=None)
        _create_module_with_dep(driver, "modx", repo="repo_x", depends_on="base")
        _create_module_with_dep(driver, "mody", repo="repo_y", depends_on="base")

        result = find_dependent_repos(driver, TEST_VERSION, {"base"})
        assert "repo_x" in result, f"repo_x should be in result; got: {result}"
        assert "repo_y" in result, f"repo_y should be in result; got: {result}"


# ---------------------------------------------------------------------------
# Tests for reset_head_sha + get_repo_ids_by_local_path_basenames (PG only)
# ---------------------------------------------------------------------------

class TestResetHeadSha:
    """Unit tests for repo_registry.reset_head_sha."""

    def test_nulls_head_sha_for_given_ids(self, pg_conn):
        """reset_head_sha(conn, [id]) sets head_sha to NULL."""
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_head_sha,
            reset_head_sha,
            update_repo_head_sha,
        )

        run_migrations(pg_conn)

        profile_id = add_profile(pg_conn, "_reset_test_profile", TEST_VERSION)
        repo_id = add_repo(
            pg_conn, profile_id,
            url="file://reset-test",
            branch=TEST_VERSION,
            local_path="/tmp/reset_test_repo_a",
        )
        # Set head_sha to something non-null
        update_repo_head_sha(pg_conn, repo_id, "deadbeef01")

        n = reset_head_sha(pg_conn, [repo_id])
        assert n == 1, f"Expected 1 row updated, got {n}"
        assert get_repo_head_sha(pg_conn, repo_id) is None, (
            "head_sha should be NULL after reset_head_sha"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = %s", (repo_id,))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))

    def test_empty_list_returns_zero(self, pg_conn):
        """reset_head_sha with empty list is a no-op returning 0."""
        from src.db.repo_registry import reset_head_sha

        n = reset_head_sha(pg_conn, [])
        assert n == 0

    def test_bulk_reset_multiple_repos(self, pg_conn):
        """reset_head_sha resets multiple repos in one UPDATE."""
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_head_sha,
            reset_head_sha,
            update_repo_head_sha,
        )

        run_migrations(pg_conn)

        profile_id = add_profile(pg_conn, "_bulk_reset_profile", TEST_VERSION)
        id_b = add_repo(
            pg_conn, profile_id,
            url="file://bulk-b",
            branch=TEST_VERSION,
            local_path="/tmp/bulk_test_repo_b",
        )
        id_c = add_repo(
            pg_conn, profile_id,
            url="file://bulk-c",
            branch=TEST_VERSION,
            local_path="/tmp/bulk_test_repo_c",
        )
        update_repo_head_sha(pg_conn, id_b, "aaaa1111")
        update_repo_head_sha(pg_conn, id_c, "bbbb2222")

        n = reset_head_sha(pg_conn, [id_b, id_c])
        assert n == 2, f"Expected 2 rows updated, got {n}"
        assert get_repo_head_sha(pg_conn, id_b) is None
        assert get_repo_head_sha(pg_conn, id_c) is None

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = ANY(%s)", ([id_b, id_c],))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))


class TestGetRepoIdsByBasename:
    """Unit tests for repo_registry.get_repo_ids_by_local_path_basenames."""

    def test_maps_basename_to_id(self, pg_conn):
        """Given a basename matching local_path, returns the correct repo ID."""
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_ids_by_local_path_basenames,
        )

        run_migrations(pg_conn)

        profile_id = add_profile(pg_conn, "_basename_test_profile", TEST_VERSION)
        repo_id = add_repo(
            pg_conn, profile_id,
            url="file://basename-test",
            branch=TEST_VERSION,
            local_path="/home/user/git/odoo_17.0",
        )

        result = get_repo_ids_by_local_path_basenames(pg_conn, ["odoo_17.0"])
        assert repo_id in result, (
            f"Expected repo_id {repo_id} in result; got: {result}"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = %s", (repo_id,))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))

    def test_empty_list_returns_empty(self, pg_conn):
        """Empty basenames list → empty result without query error."""
        from src.db.repo_registry import get_repo_ids_by_local_path_basenames

        result = get_repo_ids_by_local_path_basenames(pg_conn, [])
        assert result == []

    def test_basename_collision_resets_both(self, pg_conn):
        """Two repos with same basename (e.g. /srv/odoo and /home/a/odoo) both get returned.

        Trade-off documented in ADR-0007 W14 note: get_repo_ids_by_local_path_basenames
        uses a regex that strips the leading path, so two repos whose local_path share
        the same final component are BOTH returned and BOTH get head_sha reset.
        This is the safe default (over-eager reset); the fix would require storing
        full local_path in the Module.repo property instead of just the basename.
        """
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_ids_by_local_path_basenames,
        )

        run_migrations(pg_conn)

        profile_id = add_profile(pg_conn, "_collision_test_profile", TEST_VERSION)
        id_a = add_repo(
            pg_conn, profile_id,
            url="file://collision-a",
            branch=TEST_VERSION,
            local_path="/srv/odoo",
        )
        id_b = add_repo(
            pg_conn, profile_id,
            url="file://collision-b",
            branch=TEST_VERSION,
            local_path="/home/a/odoo",
        )

        # Both have basename "odoo" → both should be returned (collision behaviour)
        result = get_repo_ids_by_local_path_basenames(pg_conn, ["odoo"])
        assert id_a in result, (
            f"Expected id_a ({id_a}) in collision result; got: {result}"
        )
        assert id_b in result, (
            f"Expected id_b ({id_b}) in collision result; got: {result}"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = ANY(%s)", ([id_a, id_b],))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))


# ---------------------------------------------------------------------------
# End-to-end: _index_repo propagation via mock
# ---------------------------------------------------------------------------

class TestDepPropagationEndToEnd:
    """End-to-end propagation tests using _index_repo with mocked subsystems."""

    def _make_writer(self) -> MagicMock:
        w = MagicMock()
        w.setup_indexes.return_value = None
        w.write_results.return_value = None
        w.write_view_results.return_value = None
        w.write_js_graph_results.return_value = None
        w.gc_stale_modules.return_value = 0
        # driver is used by find_dependent_repos
        w.driver = MagicMock()
        return w

    def _make_repo_dict(self, repo_id: int, local_path: str) -> dict:
        return {
            "id": repo_id,
            "local_path": local_path,
            "odoo_version": TEST_VERSION,
            "url": "file://test",
        }

    def _make_module_info(self, name: str, path: str, repo_name: str) -> MagicMock:
        info = MagicMock()
        info.name = name
        info.odoo_version = TEST_VERSION
        info.path = path
        info.repo = repo_name
        info.depends = []
        return info

    def test_dep_propagation_resets_dependent_repo(
        self, clean_neo4j, pg_conn, tmp_path
    ):
        """Incremental run on repo A resets head_sha of repo B (B depends on A's module).

        Seed Neo4j: repo_a provides 'base', repo_b provides 'mymod' DEPENDS_ON 'base'.
        Seed PG: both repos have head_sha = 'deadbeef'.
        Simulate incremental run on repo_a with changed_module_names = {'base'}.
        Assert: repo_b.head_sha is NULL; repo_a.head_sha stays at new value.
        """
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_head_sha,
            update_repo_head_sha,
        )
        from src.indexer.pipeline import _index_repo

        driver = clean_neo4j
        run_migrations(pg_conn)

        # Seed Neo4j: base in repo_a, mymod in repo_b depends on base
        _create_module_with_dep(driver, "base", repo="repo_a_dir", depends_on=None)
        _create_module_with_dep(driver, "mymod", repo="repo_b_dir", depends_on="base")

        # Seed PG: two repos with head_sha set
        profile_id = add_profile(pg_conn, "_e2e_prop_profile", TEST_VERSION)
        repo_a_path = str(tmp_path / "repo_a_dir")
        repo_b_path = str(tmp_path / "repo_b_dir")
        (tmp_path / "repo_a_dir").mkdir()
        (tmp_path / "repo_b_dir").mkdir()

        repo_a_id = add_repo(
            pg_conn, profile_id,
            url="file://a",
            branch=TEST_VERSION,
            local_path=repo_a_path,
        )
        repo_b_id = add_repo(
            pg_conn, profile_id,
            url="file://b",
            branch=TEST_VERSION,
            local_path=repo_b_path,
        )
        update_repo_head_sha(pg_conn, repo_a_id, "deadbeef")
        update_repo_head_sha(pg_conn, repo_b_id, "deadbeef")

        # Module info for 'base' (the changed module in repo_a)
        base_info = self._make_module_info("base", repo_a_path + "/base", "repo_a_dir")
        fake_registry = {TEST_VERSION: {"base": base_info}}

        writer = self._make_writer()
        # Wire driver to actual Neo4j so find_dependent_repos runs a real query
        writer.driver = driver

        repo_a = self._make_repo_dict(repo_a_id, repo_a_path)

        old_head = "deadbeef"
        new_head = "cafebabe"

        with (
            patch("src.indexer.pipeline.build_registry", return_value=fake_registry),
            patch("src.indexer.pipeline._incremental.get_repo_head", return_value=new_head),
            patch("src.indexer.pipeline._repo_registry.get_repo_head_sha", return_value=old_head),
            patch("src.indexer.pipeline._incremental.is_ancestor", return_value=True),
            patch(
                "src.indexer.pipeline._incremental.compute_changed_module_paths",
                return_value={"base"},
            ),
            patch(
                "src.indexer.pipeline._incremental.filter_modules_by_changed",
                return_value={"base": base_info},
            ),
            patch("src.indexer.pipeline.topological_sort", return_value=["base"]),
            patch("src.indexer.pipeline.parser_python.parse_module", return_value=MagicMock(
                module=base_info, models=[],
            )),
            patch("src.indexer.pipeline.parser_xml.parse_module", return_value=MagicMock(views=[])),
            patch("src.indexer.pipeline.parser_qweb.parse_module", return_value=MagicMock(qweb=[])),
            patch("src.indexer.pipeline.parser_js.parse_module_graph", return_value=MagicMock(
                patches=[], components=[],
            )),
        ):
            _index_repo(repo_a, writer, pg_conn=pg_conn, full_reindex=False)

        # repo_b head_sha should be NULL (propagation reset it)
        repo_b_sha = get_repo_head_sha(pg_conn, repo_b_id)
        assert repo_b_sha is None, (
            f"repo_b head_sha should be NULL after propagation; got: {repo_b_sha}"
        )

        # repo_a head_sha should be updated to new_head (not reset)
        repo_a_sha = get_repo_head_sha(pg_conn, repo_a_id)
        assert repo_a_sha == new_head, (
            f"repo_a head_sha should be {new_head!r}; got: {repo_a_sha}"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = ANY(%s)", ([repo_a_id, repo_b_id],))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))

    def test_dep_propagation_skips_unrelated_repo(
        self, clean_neo4j, pg_conn, tmp_path
    ):
        """Repo C's module has no DEPENDS_ON to base → C's head_sha is unchanged."""
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_head_sha,
            update_repo_head_sha,
        )
        from src.indexer.pipeline import _index_repo

        driver = clean_neo4j
        run_migrations(pg_conn)

        # Seed Neo4j: base in repo_a, unrelated in repo_c (no DEPENDS_ON)
        _create_module_with_dep(driver, "base", repo="repo_a_dir2", depends_on=None)
        _create_module_with_dep(driver, "unrelated", repo="repo_c_dir", depends_on=None)

        profile_id = add_profile(pg_conn, "_e2e_skip_profile", TEST_VERSION)
        repo_a_path = str(tmp_path / "repo_a_dir2")
        repo_c_path = str(tmp_path / "repo_c_dir")
        (tmp_path / "repo_a_dir2").mkdir()
        (tmp_path / "repo_c_dir").mkdir()

        repo_a_id = add_repo(
            pg_conn, profile_id,
            url="file://a2",
            branch=TEST_VERSION,
            local_path=repo_a_path,
        )
        repo_c_id = add_repo(
            pg_conn, profile_id,
            url="file://c",
            branch=TEST_VERSION,
            local_path=repo_c_path,
        )
        update_repo_head_sha(pg_conn, repo_a_id, "deadbeef")
        update_repo_head_sha(pg_conn, repo_c_id, "deadbeef")

        base_info = self._make_module_info("base", repo_a_path + "/base", "repo_a_dir2")
        fake_registry = {TEST_VERSION: {"base": base_info}}

        writer = self._make_writer()
        writer.driver = driver

        repo_a = self._make_repo_dict(repo_a_id, repo_a_path)

        old_head = "deadbeef"
        new_head = "cafebabe"

        with (
            patch("src.indexer.pipeline.build_registry", return_value=fake_registry),
            patch("src.indexer.pipeline._incremental.get_repo_head", return_value=new_head),
            patch("src.indexer.pipeline._repo_registry.get_repo_head_sha", return_value=old_head),
            patch("src.indexer.pipeline._incremental.is_ancestor", return_value=True),
            patch(
                "src.indexer.pipeline._incremental.compute_changed_module_paths",
                return_value={"base"},
            ),
            patch(
                "src.indexer.pipeline._incremental.filter_modules_by_changed",
                return_value={"base": base_info},
            ),
            patch("src.indexer.pipeline.topological_sort", return_value=["base"]),
            patch("src.indexer.pipeline.parser_python.parse_module", return_value=MagicMock(
                module=base_info, models=[],
            )),
            patch("src.indexer.pipeline.parser_xml.parse_module", return_value=MagicMock(views=[])),
            patch("src.indexer.pipeline.parser_qweb.parse_module", return_value=MagicMock(qweb=[])),
            patch("src.indexer.pipeline.parser_js.parse_module_graph", return_value=MagicMock(
                patches=[], components=[],
            )),
        ):
            _index_repo(repo_a, writer, pg_conn=pg_conn, full_reindex=False)

        # repo_c must NOT be reset — it has no dependency on base
        repo_c_sha = get_repo_head_sha(pg_conn, repo_c_id)
        assert repo_c_sha == "deadbeef", (
            f"repo_c head_sha should remain 'deadbeef'; got: {repo_c_sha}"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = ANY(%s)", ([repo_a_id, repo_c_id],))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))

    def test_dep_propagation_skips_self(self, clean_neo4j, pg_conn, tmp_path):
        """The repo being indexed must NOT reset its own head_sha via propagation.

        Scenario: repo_a has 'base' which self-depends (or another module in
        the same repo depends on it).  The repo we just indexed (repo_a) must
        end up with the new head_sha, NOT NULL.
        """
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_head_sha,
            update_repo_head_sha,
        )
        from src.indexer.pipeline import _index_repo

        driver = clean_neo4j
        run_migrations(pg_conn)

        # Seed Neo4j: both 'base' and 'sale' in the same repo_a_dir3
        _create_module_with_dep(driver, "base", repo="repo_a_dir3", depends_on=None)
        _create_module_with_dep(driver, "sale", repo="repo_a_dir3", depends_on="base")

        profile_id = add_profile(pg_conn, "_e2e_self_profile", TEST_VERSION)
        repo_a_path = str(tmp_path / "repo_a_dir3")
        (tmp_path / "repo_a_dir3").mkdir()

        repo_a_id = add_repo(
            pg_conn, profile_id,
            url="file://a3",
            branch=TEST_VERSION,
            local_path=repo_a_path,
        )
        update_repo_head_sha(pg_conn, repo_a_id, "deadbeef")

        base_info = self._make_module_info("base", repo_a_path + "/base", "repo_a_dir3")
        fake_registry = {TEST_VERSION: {"base": base_info}}

        writer = self._make_writer()
        writer.driver = driver

        repo_a = self._make_repo_dict(repo_a_id, repo_a_path)

        old_head = "deadbeef"
        new_head = "cafebabe"

        with (
            patch("src.indexer.pipeline.build_registry", return_value=fake_registry),
            patch("src.indexer.pipeline._incremental.get_repo_head", return_value=new_head),
            patch("src.indexer.pipeline._repo_registry.get_repo_head_sha", return_value=old_head),
            patch("src.indexer.pipeline._incremental.is_ancestor", return_value=True),
            patch(
                "src.indexer.pipeline._incremental.compute_changed_module_paths",
                return_value={"base"},
            ),
            patch(
                "src.indexer.pipeline._incremental.filter_modules_by_changed",
                return_value={"base": base_info},
            ),
            patch("src.indexer.pipeline.topological_sort", return_value=["base"]),
            patch("src.indexer.pipeline.parser_python.parse_module", return_value=MagicMock(
                module=base_info, models=[],
            )),
            patch("src.indexer.pipeline.parser_xml.parse_module", return_value=MagicMock(views=[])),
            patch("src.indexer.pipeline.parser_qweb.parse_module", return_value=MagicMock(qweb=[])),
            patch("src.indexer.pipeline.parser_js.parse_module_graph", return_value=MagicMock(
                patches=[], components=[],
            )),
        ):
            _index_repo(repo_a, writer, pg_conn=pg_conn, full_reindex=False)

        # repo_a must NOT be reset — it was the one we just indexed
        repo_a_sha = get_repo_head_sha(pg_conn, repo_a_id)
        assert repo_a_sha == new_head, (
            f"repo_a head_sha should be {new_head!r} (not reset); got: {repo_a_sha}"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = %s", (repo_a_id,))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))

    def test_full_reindex_skips_propagation(
        self, clean_neo4j, pg_conn, tmp_path
    ):
        """full_reindex=True skips cross-repo propagation (no head_sha reset)."""
        from src.db.migrate import run_migrations
        from src.db.repo_registry import (
            add_profile,
            add_repo,
            get_repo_head_sha,
            update_repo_head_sha,
        )
        from src.indexer.pipeline import _index_repo

        driver = clean_neo4j
        run_migrations(pg_conn)

        _create_module_with_dep(driver, "base", repo="repo_a_dir4", depends_on=None)
        _create_module_with_dep(driver, "mymod", repo="repo_b_dir4", depends_on="base")

        profile_id = add_profile(pg_conn, "_e2e_full_profile", TEST_VERSION)
        repo_a_path = str(tmp_path / "repo_a_dir4")
        repo_b_path = str(tmp_path / "repo_b_dir4")
        (tmp_path / "repo_a_dir4").mkdir()
        (tmp_path / "repo_b_dir4").mkdir()

        repo_a_id = add_repo(
            pg_conn, profile_id,
            url="file://a4",
            branch=TEST_VERSION,
            local_path=repo_a_path,
        )
        repo_b_id = add_repo(
            pg_conn, profile_id,
            url="file://b4",
            branch=TEST_VERSION,
            local_path=repo_b_path,
        )
        update_repo_head_sha(pg_conn, repo_a_id, "deadbeef")
        update_repo_head_sha(pg_conn, repo_b_id, "deadbeef")

        base_info = self._make_module_info("base", repo_a_path + "/base", "repo_a_dir4")
        fake_registry = {TEST_VERSION: {"base": base_info}}

        writer = self._make_writer()
        writer.driver = driver

        repo_a = self._make_repo_dict(repo_a_id, repo_a_path)
        new_head = "cafebabe"

        with (
            patch("src.indexer.pipeline.build_registry", return_value=fake_registry),
            patch("src.indexer.pipeline._incremental.get_repo_head", return_value=new_head),
            patch("src.indexer.pipeline.topological_sort", return_value=["base"]),
            patch("src.indexer.pipeline.parser_python.parse_module", return_value=MagicMock(
                module=base_info, models=[],
            )),
            patch("src.indexer.pipeline.parser_xml.parse_module", return_value=MagicMock(views=[])),
            patch("src.indexer.pipeline.parser_qweb.parse_module", return_value=MagicMock(qweb=[])),
            patch("src.indexer.pipeline.parser_js.parse_module_graph", return_value=MagicMock(
                patches=[], components=[],
            )),
        ):
            _index_repo(repo_a, writer, pg_conn=pg_conn, full_reindex=True)

        # repo_b head_sha must remain unchanged (full reindex skips propagation)
        repo_b_sha = get_repo_head_sha(pg_conn, repo_b_id)
        assert repo_b_sha == "deadbeef", (
            f"repo_b head_sha should remain 'deadbeef' after full reindex; got: {repo_b_sha}"
        )

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM repos WHERE id = ANY(%s)", ([repo_a_id, repo_b_id],))
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))
