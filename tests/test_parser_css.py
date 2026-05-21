# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for parser_css.py — CSS file parsing for Odoo modules (WI-A1).

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


def _write_css(tmp_path: Path, content: str, filename: str = "styles.css") -> Path:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


class TestParseCSSSimple:
    """Parse a simple CSS file with selector groups and variable declarations."""

    def test_parse_simple_selectors(self, tmp_path):
        from src.indexer.parser_css import parse_file

        css = """\
        .o_form_view {
            background-color: #fff;
            padding: 16px;
        }
        .o_list_view {
            border: 1px solid #ccc;
        }
        """
        css_file = _write_css(tmp_path, css)
        module = _make_module(path=str(tmp_path))

        chunks, info = parse_file(str(css_file), module)

        assert info.language == "css"
        assert info.module == "test_module"
        assert info.odoo_version == "17.0"
        assert info.selector_count >= 2, f"Expected ≥2 selectors, got {info.selector_count}"
        assert len(chunks) >= 2, f"Expected ≥2 chunks, got {len(chunks)}"
        # All chunks must have correct type and module
        for c in chunks:
            assert c.module == "test_module"
            assert c.odoo_version == "17.0"
            assert c.file_path == str(css_file)
            assert c.content.strip()

    def test_parse_css_custom_properties(self, tmp_path):
        """CSS custom property declarations should increment variable_count."""
        from src.indexer.parser_css import parse_file

        css = """\
        :root {
            --primary-color: #875A7B;
            --font-size-base: 14px;
            --o-border-radius: 4px;
        }
        """
        css_file = _write_css(tmp_path, css)
        module = _make_module()

        chunks, info = parse_file(str(css_file), module)

        assert info.variable_count >= 1, (
            f"Expected ≥1 variable, got {info.variable_count}. "
            "CSS custom properties should be counted."
        )
        assert len(chunks) >= 1

    def test_parse_css_imports(self, tmp_path):
        """@import directives should increment import_count and populate imports."""
        from src.indexer.parser_css import parse_file

        css = """\
        @import "variables.css";
        @import url("theme.css");

        .widget {
            color: red;
        }
        """
        css_file = _write_css(tmp_path, css)
        module = _make_module()

        chunks, info = parse_file(str(css_file), module)

        assert info.import_count >= 1, f"Expected ≥1 import, got {info.import_count}"
        # Should have at least one 'import' chunk
        import_chunks = [c for c in chunks if c.chunk_kind == "import"]
        assert len(import_chunks) >= 1, "Expected at least one 'import' chunk"

    def test_sliding_chunks_large_file(self, tmp_path):
        """Large CSS blocks must be split into overlapping window chunks."""
        from src.indexer.parser_css import _WINDOW, parse_file

        # Generate a CSS file where a single selector block exceeds _WINDOW
        long_comment = "/* " + ("x" * 100 + "\n") * 25 + " */"
        css = f".o_huge_selector {{\n{long_comment}\n    color: red;\n}}\n"
        css_file = _write_css(tmp_path, css)
        module = _make_module()

        chunks, info = parse_file(str(css_file), module)

        assert len(chunks) >= 1
        for c in chunks:
            assert len(c.content) <= _WINDOW + 1, (
                f"Chunk content exceeded window size: {len(c.content)}"
            )

    def test_empty_css_file(self, tmp_path):
        """Empty CSS file should produce a single raw chunk, not crash."""
        from src.indexer.parser_css import parse_file

        css_file = _write_css(tmp_path, "")
        module = _make_module()

        chunks, info = parse_file(str(css_file), module)

        assert isinstance(chunks, list)
        assert info.language == "css"

    def test_media_query_chunk_kind(self, tmp_path):
        """@media blocks should produce chunks with chunk_kind='media'."""
        from src.indexer.parser_css import parse_file

        css = """\
        @media (max-width: 768px) {
            .o_form_view {
                padding: 8px;
            }
        }
        """
        css_file = _write_css(tmp_path, css)
        module = _make_module()

        chunks, info = parse_file(str(css_file), module)

        media_chunks = [c for c in chunks if c.chunk_kind == "media"]
        assert len(media_chunks) >= 1, (
            f"Expected at least one 'media' chunk, got chunk_kinds: "
            f"{[c.chunk_kind for c in chunks]}"
        )

    def test_parse_module_scans_static_dir(self, tmp_path):
        """parse_module should find .css files under static/."""
        from src.indexer.parser_css import parse_module

        # Create module directory structure
        module_dir = tmp_path / "test_module"
        static_src = module_dir / "static" / "src"
        static_src.mkdir(parents=True)
        (static_src / "styles.css").write_text(".foo { color: red; }", encoding="utf-8")
        (static_src / "theme.css").write_text(".bar { color: blue; }", encoding="utf-8")

        module = _make_module(path=str(module_dir))
        chunks, infos = parse_module(module)

        assert len(infos) == 2, f"Expected 2 StylesheetInfo, got {len(infos)}"
        assert len(chunks) >= 2

    def test_stylesheet_info_fields(self, tmp_path):
        """StylesheetInfo should have correct file_path, module, version."""
        from src.indexer.parser_css import parse_file

        css = ".x { color: red; }"
        css_file = _write_css(tmp_path, css)
        module = _make_module(name="sale", version="16.0")

        _, info = parse_file(str(css_file), module)

        assert info.file_path == str(css_file)
        assert info.module == "sale"
        assert info.odoo_version == "16.0"
        assert info.language == "css"
        assert info.mixin_count == 0  # CSS never has mixins
