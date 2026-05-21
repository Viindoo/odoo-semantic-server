# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_neo4j_scoped_delete.py
"""Integration tests for Neo4jWriter.delete_modules_scoped (M8 W0).

Seeds two repos with the same odoo_version but different repo properties,
then verifies that deleting one repo's modules leaves the other intact.
"""
import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(neo4j_driver):
    """Return a Neo4jWriter connected to the test Neo4j instance."""
    import os

    from src.indexer.writer_neo4j import Neo4jWriter

    uri = os.environ["NEO4J_URI"]
    user = os.environ["NEO4J_USER"]
    password = os.environ["NEO4J_PASSWORD"]
    w = Neo4jWriter(uri, user, password)
    yield w
    w.close()


def _seed_repo(driver, repo_basename: str, odoo_version: str) -> None:
    """Seed a Module node + child Model and Field nodes for testing."""
    with driver.session() as session:
        session.run(
            """
            MERGE (m:Module {name: $mod_name, odoo_version: $v})
            SET m.repo = $repo, m.path = '/fake/path'
            """,
            mod_name=f"module_{repo_basename}",
            v=odoo_version,
            repo=repo_basename,
        )
        # Child Model node (uses module property for scoping)
        session.run(
            """
            MERGE (mdl:Model {name: $model_name, module: $mod_name, odoo_version: $v})
            """,
            model_name=f"model_{repo_basename}",
            mod_name=f"module_{repo_basename}",
            v=odoo_version,
        )
        # Child Field node
        session.run(
            """
            MERGE (f:Field {name: $field_name, model: $model_name,
                            module: $mod_name, odoo_version: $v})
            """,
            field_name=f"field_{repo_basename}",
            model_name=f"model_{repo_basename}",
            mod_name=f"module_{repo_basename}",
            v=odoo_version,
        )


def _count_nodes_for_repo(driver, repo_basename: str, odoo_version: str) -> dict:
    """Return counts of Module + Model + Field nodes for a given repo."""
    mod_name = f"module_{repo_basename}"
    with driver.session() as session:
        modules = session.run(
            "MATCH (m:Module {repo: $repo, odoo_version: $v}) RETURN count(m) AS n",
            repo=repo_basename, v=odoo_version,
        ).single()["n"]

        models = session.run(
            "MATCH (m:Model {module: $mod, odoo_version: $v}) RETURN count(m) AS n",
            mod=mod_name, v=odoo_version,
        ).single()["n"]

        fields = session.run(
            "MATCH (f:Field {module: $mod, odoo_version: $v}) RETURN count(f) AS n",
            mod=mod_name, v=odoo_version,
        ).single()["n"]

    return {"modules": modules, "models": models, "fields": fields}


class TestDeleteModulesScoped:
    def test_deletes_target_repo_modules_and_children(self, clean_neo4j, writer):
        """delete_modules_scoped removes Module + Model + Field for the target repo."""
        driver = clean_neo4j

        _seed_repo(driver, "repo_A", TEST_VERSION)
        _seed_repo(driver, "repo_B", TEST_VERSION)

        # Verify both are seeded
        assert _count_nodes_for_repo(driver, "repo_A", TEST_VERSION)["modules"] == 1
        assert _count_nodes_for_repo(driver, "repo_B", TEST_VERSION)["modules"] == 1

        result = writer.delete_modules_scoped("repo_A", TEST_VERSION)

        assert result["modules"] == 1
        assert result["children"] >= 2  # 1 Model + 1 Field

    def test_leaves_other_repo_intact(self, clean_neo4j, writer):
        """delete_modules_scoped does NOT remove modules/children of other repos."""
        driver = clean_neo4j

        _seed_repo(driver, "repo_A", TEST_VERSION)
        _seed_repo(driver, "repo_B", TEST_VERSION)

        writer.delete_modules_scoped("repo_A", TEST_VERSION)

        # repo_A should be gone
        counts_a = _count_nodes_for_repo(driver, "repo_A", TEST_VERSION)
        assert counts_a["modules"] == 0
        assert counts_a["models"] == 0
        assert counts_a["fields"] == 0

        # repo_B should be intact
        counts_b = _count_nodes_for_repo(driver, "repo_B", TEST_VERSION)
        assert counts_b["modules"] == 1
        assert counts_b["models"] == 1
        assert counts_b["fields"] == 1

    def test_returns_zero_when_no_modules_match(self, clean_neo4j, writer):
        """delete_modules_scoped returns zeros when no Module matches the criteria."""
        result = writer.delete_modules_scoped("nonexistent_repo", TEST_VERSION)
        assert result == {"modules": 0, "children": 0}

    def test_does_not_affect_different_version(self, clean_neo4j, writer):
        """Modules for a different odoo_version are untouched."""
        driver = clean_neo4j
        other_version = "98.0"

        _seed_repo(driver, "repo_X", TEST_VERSION)

        # Seed a node with a different version manually
        with driver.session() as session:
            session.run(
                """
                MERGE (m:Module {name: 'module_repo_X', odoo_version: $v})
                SET m.repo = 'repo_X', m.path = '/fake'
                """,
                v=other_version,
            )

        # Delete only TEST_VERSION
        writer.delete_modules_scoped("repo_X", TEST_VERSION)

        # other_version node should still exist
        with driver.session() as session:
            row = session.run(
                "MATCH (m:Module {name: 'module_repo_X', odoo_version: $v}) RETURN count(m) AS n",
                v=other_version,
            ).single()
        assert row["n"] == 1

        # Cleanup other_version manually (clean_neo4j only wipes TEST_VERSION)
        with driver.session() as session:
            session.run(
                "MATCH (m:Module {name: 'module_repo_X', odoo_version: $v}) DETACH DELETE m",
                v=other_version,
            )
