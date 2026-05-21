# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_is_definition_sale_order.py
"""Integration tests for is_definition OR-semantics against a live Neo4j instance.

Simulates the real production scenario that caused CRIT-2A / F1:
  - A module has BOTH a definition class (models/sale_order.py: _name='foo.bar')
    AND an extension class in the same module (populate/sale_order.py: _inherit='foo.bar').
  - The extension class is written AFTER the definition class (alphabetical file order:
    models/ before populate/).
  - After both writes the Model node in the definition module must have is_definition=TRUE.

All test data uses TEST_VERSION='99.0' (CLAUDE.md convention).
"""
import os

import pytest

from src.indexer.models import ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to the test Neo4j instance."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _make_module(name: str) -> ModuleInfo:
    return ModuleInfo(
        name=name,
        odoo_version=TEST_VERSION,
        repo=f"{name}_repo",
        path=f"/tmp/{name}",
        depends=[],
        version_raw="",
    )


class TestIsDefinitionSaleOrderScenario:
    """Reproduce the F1 scenario: definition file + populate extension in same module."""

    def test_definition_then_extension_in_same_module_keeps_true(
        self, writer, neo4j_driver
    ):
        """Core F1 scenario:
        1. Write definition class (models/sale_order.py): _name='foo.bar', no self-inherit
        2. Write extension class (populate/sale_order.py): only _inherit='foo.bar'
        Both share the same Odoo module 'foo_module'.
        After both writes: Model(name='foo.bar', module='foo_module') must have is_definition=TRUE.
        """
        module = _make_module("foo_module")

        # First write: definition class — had_explicit_name=True, inherit has OTHER models
        definition_model = ModelInfo(
            name="foo.bar",
            module="foo_module",
            odoo_version=TEST_VERSION,
            had_explicit_name=True,
            inherit=["mail.thread", "portal.mixin"],  # no self-inherit
        )

        # Second write: extension class — had_explicit_name=False (no _name)
        extension_model = ModelInfo(
            name="foo.bar",   # derived from inherit[0] by parser
            module="foo_module",
            odoo_version=TEST_VERSION,
            had_explicit_name=False,
            inherit=["foo.bar"],  # self-inherit → incoming is_definition = FALSE
        )

        # Write definition first, then extension (mirrors alphabetical file order)
        writer.write_results([ParseResult(module=module, models=[definition_model])])
        writer.write_results([ParseResult(module=module, models=[extension_model])])

        with neo4j_driver.session() as session:
            rec = session.run(
                """
                MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                RETURN m.is_definition AS is_def,
                       m.had_explicit_name AS had_name
                """,
                name="foo.bar", mod="foo_module", v=TEST_VERSION,
            ).single()

        assert rec is not None, "Model node must exist after two writes"
        assert rec["is_def"] is True, (
            "is_definition must stay TRUE after extension class overwrites "
            "(F1 regression: populate/sale_order.py clobbered is_definition)"
        )
        assert rec["had_name"] is True, (
            "had_explicit_name must stay TRUE — extension class must not clobber it"
        )

    def test_extension_then_definition_in_same_module_becomes_true(
        self, writer, neo4j_driver
    ):
        """Reverse order: extension class indexed before definition class.

        Even if populate/ is processed before models/ (hypothetically), once the
        definition class is written the node must flip to is_definition=TRUE.
        """
        module = _make_module("bar_module")

        # First write: extension class
        extension_model = ModelInfo(
            name="bar.baz",
            module="bar_module",
            odoo_version=TEST_VERSION,
            had_explicit_name=False,
            inherit=["bar.baz"],
        )
        # Second write: definition class
        definition_model = ModelInfo(
            name="bar.baz",
            module="bar_module",
            odoo_version=TEST_VERSION,
            had_explicit_name=True,
            inherit=["mail.thread"],
        )

        writer.write_results([ParseResult(module=module, models=[extension_model])])
        writer.write_results([ParseResult(module=module, models=[definition_model])])

        with neo4j_driver.session() as session:
            rec = session.run(
                """
                MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                RETURN m.is_definition AS is_def
                """,
                name="bar.baz", mod="bar_module", v=TEST_VERSION,
            ).single()

        assert rec is not None
        assert rec["is_def"] is True

    def test_pure_extension_module_stays_false(self, writer, neo4j_driver):
        """A separate module that only has an extension class is correctly FALSE.

        This verifies OR semantics don't accidentally flip unrelated extensions to TRUE.
        The sale module defines foo.bar (TRUE), but ext_module only inherits it (FALSE).
        """
        # sale module: defines foo.bar
        sale_module = _make_module("sale_mod")
        sale_def_model = ModelInfo(
            name="foo.bar",
            module="sale_mod",
            odoo_version=TEST_VERSION,
            had_explicit_name=True,
            inherit=["mail.thread"],
        )

        # ext_module: only extends foo.bar (no _name, no definition)
        ext_module = _make_module("ext_mod")
        ext_model = ModelInfo(
            name="foo.bar",
            module="ext_mod",
            odoo_version=TEST_VERSION,
            had_explicit_name=False,
            inherit=["foo.bar"],
        )

        writer.write_results([ParseResult(module=sale_module, models=[sale_def_model])])
        writer.write_results([ParseResult(module=ext_module, models=[ext_model])])

        with neo4j_driver.session() as session:
            rows = session.run(
                """
                MATCH (m:Model {name: $name, odoo_version: $v})
                RETURN m.module AS mod, m.is_definition AS is_def
                ORDER BY m.module
                """,
                name="foo.bar", v=TEST_VERSION,
            ).data()

        mods = {r["mod"]: r["is_def"] for r in rows}
        assert mods.get("sale_mod") is True, "Definition module must be TRUE"
        assert mods.get("ext_mod") is False, (
            "Extension-only module must remain FALSE — OR logic is per-node, not global"
        )

    def test_idempotent_double_write_of_definition(self, writer, neo4j_driver):
        """Writing the same definition class twice does not change is_definition=TRUE."""
        module = _make_module("idempotent_mod")
        model = ModelInfo(
            name="idempotent.model",
            module="idempotent_mod",
            odoo_version=TEST_VERSION,
            had_explicit_name=True,
            inherit=["mail.thread"],
        )

        writer.write_results([ParseResult(module=module, models=[model])])
        writer.write_results([ParseResult(module=module, models=[model])])

        with neo4j_driver.session() as session:
            rec = session.run(
                """
                MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                RETURN m.is_definition AS is_def
                """,
                name="idempotent.model", mod="idempotent_mod", v=TEST_VERSION,
            ).single()

        assert rec is not None
        assert rec["is_def"] is True
