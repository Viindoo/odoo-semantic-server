# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for parser_less.py — LESS file parsing for Odoo v8-v11 (RP WI-3).

Unit tests cover the public API (parse_file, parse_module) without Docker.
Integration tests (marked @pytest.mark.neo4j) verify that :Stylesheet nodes with
language='less' and :IMPORTS edges are written to Neo4j when a v11-style module
containing .less files is indexed end-to-end.
"""
import textwrap
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo


def _make_module(
    name: str = "test_module",
    version: str = "11.0",
    path: str = "/tmp",
) -> ModuleInfo:
    return ModuleInfo(
        name=name,
        odoo_version=version,
        repo="test_repo",
        path=path,
        depends=[],
    )


def _write_less(tmp_path: Path, content: str, filename: str = "styles.less") -> Path:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Unit tests — no Docker required
# ---------------------------------------------------------------------------

class TestParseLessBasic:
    """Unit tests for parser_less.parse_file — regex-based extraction."""

    def test_language_is_less(self, tmp_path):
        """StylesheetInfo.language must be 'less' for .less files."""
        from src.indexer.parser_less import parse_file

        less_file = _write_less(tmp_path, ".o_form { color: red; }")
        module = _make_module()

        _, info = parse_file(str(less_file), module)

        assert info.language == "less", f"Expected 'less', got {info.language!r}"

    def test_variable_detection(self, tmp_path):
        """@var: declarations should increment variable_count and emit 'variable' chunks."""
        from src.indexer.parser_less import parse_file

        less = """\
        @primary: #875A7B;
        @secondary: #00a09d;
        @font-size-base: 14px;

        .o_widget {
            color: @primary;
            font-size: @font-size-base;
        }
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.variable_count >= 3, (
            f"Expected ≥3 LESS variables, got {info.variable_count}"
        )
        var_chunks = [c for c in chunks if c.chunk_kind == "variable"]
        assert len(var_chunks) >= 1, (
            f"Expected ≥1 'variable' chunk, got kinds: {[c.chunk_kind for c in chunks]}"
        )

    def test_import_detection(self, tmp_path):
        """@import 'file'; should increment import_count and emit 'import' chunks."""
        from src.indexer.parser_less import parse_file

        # Create target file so import resolves
        target = tmp_path / "variables.less"
        target.write_text("@brand: #875A7B;", encoding="utf-8")

        less = """\
        @import "variables";

        .o_widget {
            color: @brand;
        }
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.import_count >= 1, f"Expected ≥1 import, got {info.import_count}"
        import_chunks = [c for c in chunks if c.chunk_kind == "import"]
        assert len(import_chunks) >= 1, (
            f"Expected ≥1 'import' chunk, got kinds: {[c.chunk_kind for c in chunks]}"
        )

    def test_import_with_option(self, tmp_path):
        """@import (reference) 'file'; style import should be detected."""
        from src.indexer.parser_less import parse_file

        less = '@import (reference) "mixins";\n.o_btn { color: red; }\n'
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        _, info = parse_file(str(less_file), module)

        assert info.import_count >= 1, f"Expected ≥1 import, got {info.import_count}"

    def test_mixin_definition(self, tmp_path):
        """LESS mixin definitions .name() { } should produce 'mixin' chunks."""
        from src.indexer.parser_less import parse_file

        less = """\
        .o-flex-center() {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .o_form_view {
            .o-flex-center();
        }
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.mixin_count >= 1, f"Expected ≥1 mixin, got {info.mixin_count}"
        mixin_chunks = [c for c in chunks if c.chunk_kind == "mixin"]
        assert len(mixin_chunks) >= 1, (
            f"Expected ≥1 'mixin' chunk, got kinds: {[c.chunk_kind for c in chunks]}"
        )

    def test_selector_detection(self, tmp_path):
        """CSS-style selectors should be counted and produce 'selector' chunks."""
        from src.indexer.parser_less import parse_file

        less = """\
        .o_form_view {
            padding: 16px;
        }

        .o_list_view {
            margin: 0;
        }
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.selector_count >= 2, (
            f"Expected ≥2 selectors, got {info.selector_count}"
        )
        selector_chunks = [c for c in chunks if c.chunk_kind == "selector"]
        assert len(selector_chunks) >= 1

    def test_media_query_detection(self, tmp_path):
        """@media blocks should produce 'media' chunks."""
        from src.indexer.parser_less import parse_file

        less = """\
        @media (min-width: 768px) {
            .o_form_view {
                padding: 24px;
            }
        }
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        media_chunks = [c for c in chunks if c.chunk_kind == "media"]
        assert len(media_chunks) >= 1, (
            f"Expected ≥1 'media' chunk, got kinds: {[c.chunk_kind for c in chunks]}"
        )

    def test_import_not_confused_with_variable(self, tmp_path):
        """@import lines should NOT increment variable_count."""
        from src.indexer.parser_less import parse_file

        less = '@import "mixins";\n@primary: #875A7B;\n'
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        _, info = parse_file(str(less_file), module)

        assert info.import_count >= 1
        assert info.variable_count >= 1
        # variable_count should count only @primary, NOT @import
        assert info.variable_count == 1, (
            f"Expected exactly 1 variable (@primary), got {info.variable_count}. "
            "Is @import being miscounted as a variable?"
        )

    def test_parse_module_scans_less_in_static(self, tmp_path):
        """parse_module should find .less files under static/ recursively."""
        from src.indexer.parser_less import parse_module

        module_dir = tmp_path / "web_theme_v11"
        less_dir = module_dir / "static" / "src" / "less"
        less_dir.mkdir(parents=True)
        (less_dir / "variables.less").write_text("@primary: #875A7B;", encoding="utf-8")
        (less_dir / "components.less").write_text(
            ".o_mixin() { display: flex; }", encoding="utf-8"
        )

        module = _make_module(name="web_theme_v11", path=str(module_dir))
        chunks, infos = parse_module(module)

        assert len(infos) == 2, f"Expected 2 StylesheetInfo objects, got {len(infos)}"
        for info in infos:
            assert info.language == "less", f"Expected 'less', got {info.language!r}"
            assert info.module == "web_theme_v11"

    def test_parse_module_no_static_dir(self, tmp_path):
        """parse_module returns ([], []) when static/ does not exist."""
        from src.indexer.parser_less import parse_module

        module_dir = tmp_path / "no_static_module"
        module_dir.mkdir()
        module = _make_module(path=str(module_dir))

        chunks, infos = parse_module(module)

        assert chunks == []
        assert infos == []

    def test_import_resolution_with_extension(self, tmp_path):
        """@import 'variables.less'; resolves when file exists with explicit extension."""
        from src.indexer.parser_less import parse_file

        target = tmp_path / "variables.less"
        target.write_text("@brand: blue;", encoding="utf-8")

        less = '@import "variables.less";\n.o_w { color: @brand; }\n'
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.import_count >= 1
        # Resolved path should be the absolute path to variables.less
        assert any(str(target.resolve()) in imp for imp in info.imports), (
            f"Expected resolved path {target.resolve()} in imports {info.imports}"
        )

    def test_make_less_chunks_type_and_entity(self, tmp_path):
        """make_less_chunks should produce EmbeddingChunk with chunk_type='less'."""
        from src.indexer.parser_less import parse_file
        from src.indexer.writer_pgvector import make_less_chunks

        less = "@primary: #875A7B;\n.o_form { color: @primary; }\n"
        less_file = _write_less(tmp_path, less)
        module = _make_module()
        chunks, _ = parse_file(str(less_file), module)

        embedding_chunks = make_less_chunks(chunks)

        assert len(embedding_chunks) >= 1
        for ec in embedding_chunks:
            assert ec.chunk_type == "less", (
                f"Expected chunk_type='less', got {ec.chunk_type!r}"
            )
            # entity_name must encode kind as "kind:name"
            assert ":" in ec.entity_name, (
                f"entity_name should be 'kind:name', got: {ec.entity_name!r}"
            )

    def test_mixin_def_not_double_counted_as_selector(self, tmp_path):
        """FIX 1: A mixin definition must be counted once as mixin, NOT also as selector.

        Fixture has:
          - 1 mixin def  .foo() { ... }
          - 1 plain class selector  .bar { ... }
          - 1 pseudo-class selector  a:hover { ... }

        Expected: mixin_count==1, selector_count==2 (bar + a:hover), and no
        'selector' chunk whose entity_name matches the mixin def.
        """
        from src.indexer.parser_less import parse_file

        less = """\
        .foo() {
            display: flex;
            align-items: center;
        }
        .bar {
            color: red;
        }
        a:hover {
            text-decoration: underline;
        }
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.mixin_count == 1, (
            f"Expected mixin_count==1, got {info.mixin_count}"
        )
        assert info.selector_count == 2, (
            f"Expected selector_count==2 (.bar + a:hover), got {info.selector_count}. "
            "Mixin def .foo() is being double-counted as a selector."
        )
        # No 'selector' chunk should correspond to the mixin def
        selector_chunks = [c for c in chunks if c.chunk_kind == "selector"]
        mixin_selector_chunks = [
            c for c in selector_chunks
            if ".foo" in c.entity_name and "()" in c.entity_name
        ]
        assert mixin_selector_chunks == [], (
            f"Mixin def '.foo()' produced phantom selector chunk(s): {mixin_selector_chunks}"
        )
        # The pseudo-class selector should be present as a selector chunk
        hover_chunks = [c for c in selector_chunks if "hover" in c.entity_name]
        assert len(hover_chunks) >= 1, (
            f"Expected 'a:hover' to produce a selector chunk, got selector chunks: "
            f"{[c.entity_name for c in selector_chunks]}"
        )

    def test_at_page_not_counted_as_variable(self, tmp_path):
        """FIX 2: @page at-rule must NOT be miscounted as a LESS variable."""
        from src.indexer.parser_less import parse_file

        less = """\
        @page :first {
            margin-top: 2cm;
        }
        @primary: #875A7B;
        """
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        _, info = parse_file(str(less_file), module)

        assert info.variable_count == 1, (
            f"Expected variable_count==1 (@primary only), got {info.variable_count}. "
            "@page is being miscounted as a variable."
        )

    def test_large_less_file_sliding_window(self, tmp_path):
        """Large LESS mixin blocks should be split into overlapping window chunks."""
        from src.indexer.parser_less import _WINDOW, parse_file

        props = "\n".join(f"    property-{i}: value-{i};" for i in range(200))
        less = f".big-mixin() {{\n{props}\n}}\n"
        less_file = _write_less(tmp_path, less)
        module = _make_module()

        chunks, info = parse_file(str(less_file), module)

        assert info.mixin_count >= 1
        for c in chunks:
            assert len(c.content) <= _WINDOW + 1, (
                f"Chunk too large: {len(c.content)} > {_WINDOW + 1}"
            )


# ---------------------------------------------------------------------------
# Integration test — requires Neo4j (testcontainers or CI service container)
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
class TestLessIndexingIntegration:
    """End-to-end: index a module with .less files → verify :Stylesheet nodes in Neo4j."""

    def test_less_stylesheet_nodes_written(self, tmp_path, clean_neo4j):
        """Indexing a v11-style module with .less files writes :Stylesheet nodes
        with language='less' and constructs :IMPORTS edges when @import targets exist.
        """
        import os

        from src.indexer.models import ModuleInfo
        from src.indexer.parser_less import parse_module as parse_less_module
        from src.indexer.writer_neo4j import Neo4jWriter
        from tests.conftest import TEST_VERSION

        # NEO4J_TEST_URI is set dynamically by testcontainers (after module-level import)
        # so read from env at runtime, not from conftest module constants.
        neo4j_uri = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_TEST_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_TEST_PASSWORD", "password")

        # Build a fixture module with a .less file that imports another
        module_dir = tmp_path / "sale_less_test"
        less_dir = module_dir / "static" / "src" / "less"
        less_dir.mkdir(parents=True)

        # Create __manifest__.py so scanner would find it (not needed here — we call
        # writer directly after parsing, but it keeps the fixture realistic)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'sale_less_test', 'version': '11.0.1.0.0'}",
            encoding="utf-8",
        )

        # variables.less — imported by main.less
        vars_file = less_dir / "variables.less"
        vars_file.write_text(
            "@primary: #875A7B;\n@secondary: #00a09d;\n",
            encoding="utf-8",
        )

        # main.less — imports variables.less
        main_file = less_dir / "main.less"
        main_file.write_text(
            '@import "variables";\n.o_form_view { color: @primary; }\n',
            encoding="utf-8",
        )

        module_info = ModuleInfo(
            name="sale_less_test",
            odoo_version=TEST_VERSION,
            repo="test_repo",
            path=str(module_dir),
            depends=[],
        )

        # Parse via parser_less
        less_chunks, less_infos = parse_less_module(module_info)

        assert len(less_infos) == 2, (
            f"Expected 2 StylesheetInfo (main + variables), got {len(less_infos)}"
        )
        for info in less_infos:
            assert info.language == "less"

        # Verify import chain: main.less should list resolved variables.less path
        main_info = next(i for i in less_infos if "main" in i.file_path)
        assert main_info.import_count >= 1, "main.less should have ≥1 import"
        assert any(
            "variables" in imp for imp in main_info.imports
        ), f"Expected 'variables' in main_info.imports, got: {main_info.imports}"

        # Write to Neo4j via writer (read-at-runtime URI so testcontainers port is correct).
        # Write twice: first pass creates all :Stylesheet nodes; second pass resolves
        # :IMPORTS edges (the writer silently skips edges when the target node doesn't
        # exist yet — same limitation as the existing SCSS writer, per ADR-0025 §D3).
        writer = Neo4jWriter(uri=neo4j_uri, user=neo4j_user, password=neo4j_password)
        try:
            writer.setup_indexes()
            writer.write_stylesheets(less_infos, profiles=[])  # pass 1: create nodes
            writer.write_stylesheets(less_infos, profiles=[])  # pass 2: resolve IMPORTS
        finally:
            writer.close()

        # Assert :Stylesheet nodes with language='less' exist
        with clean_neo4j.session() as session:
            result = session.run(
                """
                MATCH (ss:Stylesheet {module: 'sale_less_test', odoo_version: $v,
                                      language: 'less'})
                RETURN count(ss) AS cnt
                """,
                v=TEST_VERSION,
            ).single()
            count = result["cnt"] if result else 0

        assert count > 0, (
            f"Expected >0 :Stylesheet nodes with language='less', got {count}"
        )

        # Assert :IMPORTS edge from main.less to variables.less
        with clean_neo4j.session() as session:
            result = session.run(
                """
                MATCH (src:Stylesheet {module: 'sale_less_test', odoo_version: $v})
                      -[:IMPORTS]->(tgt:Stylesheet {module: 'sale_less_test',
                                                    odoo_version: $v})
                RETURN count(*) AS edge_cnt
                """,
                v=TEST_VERSION,
            ).single()
            edge_count = result["edge_cnt"] if result else 0

        assert edge_count >= 1, (
            f"Expected ≥1 :IMPORTS edge between .less Stylesheet nodes, got {edge_count}"
        )
