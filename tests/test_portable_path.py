# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ADR-0037 path portability.

No Docker / Neo4j required — these exercise pure functions:
  * ``_portable_path`` (server read-side normalization safety-net)
  * ``to_repo_relative`` / ``ModuleInfo.relative_path`` (write-side relativizer)
  * ``make_chunks`` / ``make_css_chunks`` relativization + repo provenance backfill
  * ``_reconstruct_abs_path`` (stylesheet serve-side absolute reconstruction)

Behavioural intent (the business rule, not the current code): no tool output or
stored chunk may carry a server-absolute path; every path is repo-relative and
every chunk keeps its repo/module/version provenance.
"""
from pathlib import Path

import pytest

from src.indexer.models import (
    CSSChunk,
    FieldInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
    SCSSChunk,
    to_repo_relative,
)
from src.indexer.writer_pgvector import make_chunks, make_css_chunks, make_scss_chunks


def _module(path: str, repo_root: str | None) -> ModuleInfo:
    return ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0", path=path,
        depends=[], repo_id=42,
        repo_root=Path(repo_root) if repo_root else None,
    )


# ---------------------------------------------------------------------------
# to_repo_relative / ModuleInfo.relative_path
# ---------------------------------------------------------------------------

class TestToRepoRelative:
    def test_strips_repo_root_to_repo_relative(self):
        assert to_repo_relative(
            "/home/x/odoo_17.0/addons/sale/models/sale_order.py",
            "/home/x/odoo_17.0",
        ) == "addons/sale/models/sale_order.py"

    def test_idempotent_on_already_relative(self):
        # A path already relative (not under repo_root) is returned unchanged —
        # this is what makes the write-side safe to run on reindexed data.
        assert to_repo_relative(
            "addons/sale/models/sale_order.py", "/home/x/odoo_17.0",
        ) == "addons/sale/models/sale_order.py"

    def test_none_repo_root_returns_unchanged(self):
        assert to_repo_relative("/abs/p.py", None) == "/abs/p.py"

    def test_none_path_returns_none(self):
        assert to_repo_relative(None, "/home/x") is None

    def test_module_relative_path_method(self):
        mi = _module("/home/x/odoo_17.0/addons/sale", "/home/x/odoo_17.0")
        assert mi.relative_path(mi.path) == "addons/sale"
        assert mi.relative_path(
            "/home/x/odoo_17.0/addons/sale/models/x.py"
        ) == "addons/sale/models/x.py"

    def test_module_no_repo_root_is_noop(self):
        mi = _module("/abs/sale", None)
        assert mi.relative_path("/abs/sale/x.py") == "/abs/sale/x.py"


# ---------------------------------------------------------------------------
# _portable_path (read-side render safety-net)
# ---------------------------------------------------------------------------

class TestPortablePath:
    @pytest.fixture(scope="class")
    @classmethod
    def pp(cls):
        from src.mcp.server import _portable_path
        return _portable_path

    def test_repo_anchor_yields_repo_relative(self, pp):
        # Cuts THROUGH the repo dir → matches the write-side relative form.
        assert pp(
            "/home/tuan/git/odoo_17.0/addons/sale/models/x.py", repo="odoo_17.0",
        ) == "addons/sale/models/x.py"

    def test_repo_anchor_oca_root_module(self, pp):
        assert pp(
            "/srv/server-tools/auditlog/models/x.py", repo="server-tools",
        ) == "auditlog/models/x.py"

    def test_repo_anchor_uses_last_occurrence_for_nested_dirname(self, pp):
        # Parent dirs repeat the repo name (.../repos/odoo/...) — rfind anchors
        # on the LAST "/odoo/" (the repo root), so the in-repo tail is correct.
        # find() would have stripped at "repos/odoo/..." → wrong.
        assert pp(
            "/srv/odoo/repos/odoo/addons/sale/x.py", repo="odoo",
        ) == "addons/sale/x.py"

    def test_module_anchor_keeps_module_dir(self, pp):
        assert pp(
            "/opt/odoo/css_mod/static/src/scss/main.scss", module="css_mod",
        ) == "css_mod/static/src/scss/main.scss"

    def test_core_anchor_odoo(self, pp):
        assert pp("/x/y/odoo/orm/models.py") == "odoo/orm/models.py"

    def test_core_anchor_openerp_v8(self, pp):
        assert pp("/x/odoo_8.0/openerp/osv/orm.py") == "openerp/osv/orm.py"

    def test_idempotent_on_relative(self, pp):
        assert pp("addons/sale/x.py", repo="odoo_17.0") == "addons/sale/x.py"

    def test_empty_returns_empty(self, pp):
        assert pp("") == ""
        assert pp(None) == ""

    def test_last_resort_strips_leading_slash(self, pp):
        # No anchor matches → never leak a leading "/" (absolute) to the client.
        out = pp("/weird/unknown/file.py")
        assert not out.startswith("/")
        assert out == "weird/unknown/file.py"

    def test_never_leaks_home_or_opt(self, pp):
        for raw in (
            "/home/tuan/git/odoo_17.0/addons/sale/x.py",
            "/opt/odoo/odoo_17.0/odoo/orm/models.py",
        ):
            assert "/home/" not in pp(raw, repo="odoo_17.0")
            assert not pp(raw, repo="odoo_17.0").startswith("/")


# ---------------------------------------------------------------------------
# make_chunks — relativizes file_path when repo_root is set (WS-B)
# ---------------------------------------------------------------------------

class TestMakeChunksRelativization:
    def _parse_result(self, repo_root: str | None) -> ParseResult:
        mod = _module("/repo/odoo_17.0/addons/sale", repo_root)
        model = ModelInfo(
            name="sale.order", module="sale", odoo_version="17.0",
            fields=[FieldInfo("name", "char", line=3)],
            methods=[MethodInfo("create", source_code="def create(self): ...", line=10)],
        )
        model.file_path = "/repo/odoo_17.0/addons/sale/models/sale_order.py"
        return ParseResult(module=mod, models=[model])

    def test_chunks_file_path_relative_with_repo_root(self):
        chunks = make_chunks("sale", "17.0", self._parse_result("/repo/odoo_17.0"), None, None)
        assert chunks, "expected method + field chunks"
        for c in chunks:
            assert not c.file_path.startswith("/"), (
                f"chunk file_path must be repo-relative, got {c.file_path!r}"
            )
            assert c.file_path == "addons/sale/models/sale_order.py"

    def test_chunks_noop_without_repo_root(self):
        # No repo_root → stored verbatim (back-compat; read-side strips at render).
        chunks = make_chunks("sale", "17.0", self._parse_result(None), None, None)
        assert all(
            c.file_path == "/repo/odoo_17.0/addons/sale/models/sale_order.py"
            for c in chunks
        )


# ---------------------------------------------------------------------------
# make_css_chunks / make_scss_chunks — repo provenance backfill + relativize (WS-C)
# ---------------------------------------------------------------------------

class TestStylesheetChunkProvenance:
    def test_css_chunk_carries_repo_and_relative_path(self):
        mod = _module("/repo/odoo_17.0/addons/web", "/repo/odoo_17.0")
        css = CSSChunk(
            module="web", odoo_version="17.0",
            file_path="/repo/odoo_17.0/addons/web/static/src/css/a.css",
            chunk_kind="selector", entity_name=".o_form", chunk_idx=0, content="x",
        )
        out = make_css_chunks([css], mod)
        assert len(out) == 1
        chunk = out[0]
        # WS-C: provenance no longer lost when absolute path is dropped.
        assert chunk.repo == "odoo_17.0"
        assert chunk.repo_id == 42
        # ADR-0037: file_path relativized.
        assert chunk.file_path == "addons/web/static/src/css/a.css"

    def test_scss_chunk_relative_and_repo(self):
        mod = _module("/repo/odoo_17.0/addons/web", "/repo/odoo_17.0")
        scss = SCSSChunk(
            module="web", odoo_version="17.0",
            file_path="/repo/odoo_17.0/addons/web/static/src/scss/v.scss",
            chunk_kind="variable", entity_name="$o-brand", chunk_idx=0, content="x",
        )
        out = make_scss_chunks([scss], mod)
        assert out[0].repo_id == 42
        assert out[0].file_path == "addons/web/static/src/scss/v.scss"

    def test_css_chunk_backcompat_without_module_info(self):
        css = CSSChunk(
            module="web", odoo_version="17.0", file_path="/abs/web/a.css",
            chunk_kind="selector", entity_name=".x", chunk_idx=0, content="x",
        )
        out = make_css_chunks([css])
        assert out[0].repo is None and out[0].repo_id is None
        assert out[0].file_path == "/abs/web/a.css"


# ---------------------------------------------------------------------------
# _reconstruct_abs_path (stylesheet serve-side, WS-D)
# ---------------------------------------------------------------------------

class TestReconstructAbsPath:
    @pytest.fixture(scope="class")
    @classmethod
    def fn(cls):
        from src.mcp.resources import _reconstruct_abs_path
        return _reconstruct_abs_path

    def test_relative_reconstructed_from_local_path(self, fn, monkeypatch):
        class _FakeStore:
            def get_repo_by_id(self, repo_id):
                assert repo_id == 7
                return {"local_path": "/srv/clones/odoo_17.0"}

        monkeypatch.setattr("src.db.pg.repo_store", lambda: _FakeStore())
        assert fn("addons/web/static/a.scss", 7) == "/srv/clones/odoo_17.0/addons/web/static/a.scss"

    def test_absolute_legacy_path_unchanged(self, fn):
        # Legacy absolute row → opened verbatim (no repo lookup).
        assert fn("/old/abs/a.scss", 7) == "/old/abs/a.scss"

    def test_no_repo_id_returns_relative_unchanged(self, fn):
        assert fn("addons/web/a.scss", None) == "addons/web/a.scss"
