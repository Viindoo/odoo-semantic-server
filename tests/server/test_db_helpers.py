"""Unit tests for osm.server.db — no DB required."""

from __future__ import annotations

from osm.server.db import effective_indexed_at_sha, union_all
from osm.server.tenancy import context_from_tenant


def test_union_all_single_schema() -> None:
    ctx = context_from_tenant("public")
    sql = union_all("SELECT * FROM {schema}.models WHERE name = %s", ctx)
    assert "FROM public.models" in sql
    assert "UNION ALL" not in sql


def test_union_all_two_schemas() -> None:
    ctx = context_from_tenant("viindoo")
    sql = union_all("SELECT * FROM {schema}.models WHERE name = %s", ctx)
    assert "FROM public.models" in sql
    assert "FROM viindoo.models" in sql
    assert sql.count("UNION ALL") == 1


def test_effective_sha_consistent() -> None:
    assert effective_indexed_at_sha(["abc", "abc", "abc"]) == "abc"


def test_effective_sha_divergent() -> None:
    assert effective_indexed_at_sha(["abc", "def"]) is None


def test_effective_sha_empty() -> None:
    assert effective_indexed_at_sha([]) is None


def test_effective_sha_skips_blanks() -> None:
    # A blank string shouldn't mask a real consistent value.
    assert effective_indexed_at_sha(["", "abc"]) == "abc"
