# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for parser_scss.py — SCSS file parsing for Odoo modules (WI-A1).

These tests use only the public API (parse_file, parse_module) and work
regardless of whether tree-sitter-css is installed (regex fallback active).
"""
import textwrap
from pathlib import Path

from src.indexer.models import ModuleInfo


def _make_module(
    name: str = "test_module", version: str = "17.0", path: str = "/tmp"
) -> ModuleInfo:
    return ModuleInfo(
        name=name,
        odoo_version=version,
        repo="test_repo",
        path=path,
        depends=[],
    )


def _write_scss(tmp_path: Path, content: str, filename: str = "styles.scss") -> Path:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


class TestParseSCSSBasic:
    """Parse SCSS files with mixins, variables, nested rules, @extend."""

    def test_parse_mixin_definition(self, tmp_path):
        """@mixin blocks should produce 'mixin' chunks and increment mixin_count."""
        from src.indexer.parser_scss import parse_file

        scss = """\
        @mixin o_flex_center() {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .o_form_view {
            @include o_flex_center();
        }
        """
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        assert info.language == "scss"
        assert info.mixin_count >= 1, f"Expected ≥1 mixin, got {info.mixin_count}"
        mixin_chunks = [c for c in chunks if c.chunk_kind == "mixin"]
        assert len(mixin_chunks) >= 1, (
            f"Expected at least one 'mixin' chunk, got chunk_kinds: "
            f"{[c.chunk_kind for c in chunks]}"
        )

    def test_parse_scss_variables(self, tmp_path):
        """$variable declarations should increment variable_count."""
        from src.indexer.parser_scss import parse_file

        scss = """\
        $primary: #875A7B;
        $secondary: #00a09d;
        $font-size-base: 14px;

        .o_widget {
            color: $primary;
            font-size: $font-size-base;
        }
        """
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        assert info.variable_count >= 1, (
            f"Expected ≥1 SCSS variable, got {info.variable_count}"
        )

    def test_parse_include_directive(self, tmp_path):
        """@include directives should produce 'include' chunks."""
        from src.indexer.parser_scss import parse_file

        scss = """\
        .o_kanban_record {
            @include o_flex_center();
            @include o_border_radius(4px);
        }
        """
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        include_chunks = [c for c in chunks if c.chunk_kind == "include"]
        chunk_summary = [(c.chunk_kind, c.content[:40]) for c in chunks]
        assert len(include_chunks) >= 1, (
            f"Expected ≥1 'include' chunk. chunks: {chunk_summary}"
        )

    def test_parse_extend_chain(self, tmp_path):
        """@extend directives should produce 'extend' chunks."""
        from src.indexer.parser_scss import parse_file

        scss = """\
        %o_button_base {
            border-radius: 4px;
            padding: 6px 12px;
        }

        .o_btn_primary {
            @extend %o_button_base;
            background-color: #875A7B;
        }

        .o_btn_secondary {
            @extend %o_button_base;
            background-color: #00a09d;
        }
        """
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        extend_chunks = [c for c in chunks if c.chunk_kind == "extend"]
        assert len(extend_chunks) >= 1, (
            f"Expected ≥1 'extend' chunk. chunk_kinds: {[c.chunk_kind for c in chunks]}"
        )

    def test_parse_nested_rules(self, tmp_path):
        """Nested SCSS rules should be captured as selector chunks."""
        from src.indexer.parser_scss import parse_file

        scss = """\
        .o_form_view {
            padding: 16px;

            .o_field_widget {
                margin-bottom: 8px;

                &.o_field_text {
                    min-height: 60px;
                }
            }
        }
        """
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        # At least one selector chunk (outer rule set)
        selector_chunks = [c for c in chunks if c.chunk_kind == "selector"]
        assert len(selector_chunks) >= 1, (
            f"Expected ≥1 selector chunk. chunks: {[(c.chunk_kind, c.entity_name) for c in chunks]}"
        )
        assert info.selector_count >= 1

    def test_parse_scss_import_resolution(self, tmp_path):
        """@import paths should be tracked; resolved paths set when target exists."""
        from src.indexer.parser_scss import parse_file

        # Create a partial file that will be resolved
        partial = tmp_path / "_variables.scss"
        partial.write_text("$brand: #875A7B;", encoding="utf-8")

        scss = """\
        @import "variables";

        .o_widget {
            color: $brand;
        }
        """
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        assert info.import_count >= 1, f"Expected ≥1 import, got {info.import_count}"
        import_chunks = [c for c in chunks if c.chunk_kind == "import"]
        assert len(import_chunks) >= 1

    def test_parse_module_scans_scss_in_static(self, tmp_path):
        """parse_module should find .scss files under static/."""
        from src.indexer.parser_scss import parse_module

        module_dir = tmp_path / "web_theme"
        scss_dir = module_dir / "static" / "src" / "scss"
        scss_dir.mkdir(parents=True)
        (scss_dir / "variables.scss").write_text("$primary: #875A7B;", encoding="utf-8")
        (scss_dir / "components.scss").write_text(
            "@mixin flex { display: flex; }", encoding="utf-8"
        )

        module = _make_module(name="web_theme", path=str(module_dir))
        chunks, infos = parse_module(module)

        assert len(infos) == 2, f"Expected 2 StylesheetInfo objects, got {len(infos)}"
        for info in infos:
            assert info.language == "scss"
            assert info.module == "web_theme"

    def test_stylesheet_info_mixin_count_css_zero(self, tmp_path):
        """CSS parser always produces mixin_count=0 (CSS has no @mixin)."""
        from src.indexer.parser_css import parse_file

        css_file = tmp_path / "style.css"
        css_file.write_text(".foo { color: red; }", encoding="utf-8")
        module = _make_module()

        _, info = parse_file(str(css_file), module)
        assert info.mixin_count == 0

    def test_scss_chunk_entity_name_encodes_kind(self, tmp_path):
        """SCSSChunk entity_name in make_scss_chunks should encode chunk_kind."""
        from src.indexer.parser_scss import parse_file
        from src.indexer.writer_pgvector import make_scss_chunks

        scss = """\
        @mixin my_mixin() {
            color: red;
        }
        """
        scss_file = _write_scss(tmp_path, scss, "test.scss")
        module = _make_module()
        chunks, _ = parse_file(str(scss_file), module)

        embedding_chunks = make_scss_chunks(chunks)
        assert len(embedding_chunks) >= 1
        for ec in embedding_chunks:
            assert ec.chunk_type == "scss"
            # entity_name must encode kind as "kind:name"
            assert ":" in ec.entity_name, (
                f"entity_name should be 'kind:name', got: {ec.entity_name!r}"
            )

    def test_large_mixin_sliding_window(self, tmp_path):
        """Large @mixin blocks should be split into overlapping window chunks."""
        from src.indexer.parser_scss import _WINDOW, parse_file

        # Generate a mixin with lots of property declarations
        props = "\n".join(f"    property-{i}: value-{i};" for i in range(200))
        scss = f"@mixin big_mixin() {{\n{props}\n}}\n"
        scss_file = _write_scss(tmp_path, scss)
        module = _make_module()

        chunks, info = parse_file(str(scss_file), module)

        assert info.mixin_count >= 1
        for c in chunks:
            assert len(c.content) <= _WINDOW + 1


class TestParserThreadSafety:
    """Thread-safety regression tests — reviewer concern #4.

    Tree-sitter Parser objects are NOT safe for concurrent .parse() calls.
    ADR-0006 enables --profile-workers N parallel indexing; this test asserts
    that parallel parse_file() calls from N threads produce identical results
    to serial parsing, i.e. the thread-local parser pattern works.
    """

    def test_parser_css_parallel_parse_is_deterministic(self, tmp_path):
        """N threads parsing the same file in parallel produce identical output."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from src.indexer.parser_css import parse_file as parse_css

        css = (tmp_path / "x.css")
        css.write_text(
            ":root { --primary: #875A7B; --secondary: #00a09d; }\n"
            ".o_form { color: var(--primary); }\n"
            "@media (min-width: 768px) { .o_form { padding: 16px; } }\n",
            encoding="utf-8",
        )
        module = _make_module()
        # Serial baseline
        baseline_chunks, baseline_info = parse_css(str(css), module)
        baseline_kinds = tuple(c.chunk_kind for c in baseline_chunks)

        # Parallel — every thread MUST produce the same chunk sequence
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(parse_css, str(css), module) for _ in range(32)]
            results = [f.result() for f in as_completed(futures)]

        for chunks, info in results:
            assert tuple(c.chunk_kind for c in chunks) == baseline_kinds
            assert info.selector_count == baseline_info.selector_count
            assert info.variable_count == baseline_info.variable_count
            assert info.import_count == baseline_info.import_count

    def test_parser_scss_parallel_parse_is_deterministic(self, tmp_path):
        """N threads parsing the same SCSS in parallel produce identical output."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from src.indexer.parser_scss import parse_file as parse_scss

        scss = """\
        $primary: #875A7B;

        @mixin flex_center() {
            display: flex;
            align-items: center;
        }

        .o_widget {
            color: $primary;
            @include flex_center();
        }
        """
        scss_file = _write_scss(tmp_path, scss, "x.scss")
        module = _make_module()
        baseline_chunks, baseline_info = parse_scss(str(scss_file), module)
        baseline_kinds = tuple(sorted(c.chunk_kind for c in baseline_chunks))

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(parse_scss, str(scss_file), module) for _ in range(32)]
            results = [f.result() for f in as_completed(futures)]

        for chunks, info in results:
            assert tuple(sorted(c.chunk_kind for c in chunks)) == baseline_kinds
            assert info.mixin_count == baseline_info.mixin_count
            assert info.variable_count == baseline_info.variable_count
            assert info.selector_count == baseline_info.selector_count
