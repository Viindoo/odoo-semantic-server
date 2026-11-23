# SPDX-License-Identifier: AGPL-3.0-or-later
"""The curated Bootstrap / stylesheet table matches each real Odoo checkout.

``src/indexer/spec_data/bootstrap_versions.json`` feeds the public /bootstrap
page: per series, the Bootstrap version Odoo ships, the stylesheet language the
asset pipeline compiles and the stylesheet entry points a theme author edits.
Business rules checked here:

* every surveyed series has a row (CI layer, no checkout needed);
* the Bootstrap version is the one the shipped library declares in its header;
* every listed entry point exists in that series;
* the preprocessor is a language the series' asset pipeline compiles, and at
  least one non-library entry point is written in it;
* no row carries a file count: counts drift on every backport to a stable
  branch head, so a count is never true for long.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.constants import SURVEYED_MAJORS
from tests._odoo_checkouts import checkout_root

_TABLE = json.loads(
    (Path(__file__).resolve().parent.parent / "src" / "indexer" / "spec_data"
     / "bootstrap_versions.json").read_text(encoding="utf-8")
)["versions"]

_EXTENSION = {"sass": ".sass", "less": ".less", "scss": ".scss"}
# The asset-bundle class that compiles each language (ir_qweb.py / ir_asset.py).
_PIPELINE_MARKER = {"sass": "SassStylesheetAsset|SassAsset", "less": "LessStylesheetAsset",
                    "scss": "ScssStylesheetAsset"}
_HEADER = re.compile(r"Bootstrap\s+(?:\w+\.js\s+)?\(?v?(\d+\.\d+\.\d+)")


def _row(major: int) -> dict:
    return _TABLE[f"{major}.0"]


def test_every_surveyed_series_has_a_row():
    assert sorted(int(float(v)) for v in _TABLE) == list(SURVEYED_MAJORS)


def test_rows_carry_no_file_counts():
    for version, row in _TABLE.items():
        text = json.dumps(row)
        assert not re.search(r"\d+ \.(?:scss|less|sass) files", text), version


def _checkout(major: int) -> Path:
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"no Odoo {major} checkout (see tests/_odoo_checkouts.py)")
    return root


def _shipped_bootstrap_versions(root: Path) -> set[str]:
    found = set()
    for path in (root / "addons/web/static/lib/bootstrap").rglob("*"):
        if path.suffix in {".css", ".js", ".less", ".scss"} and path.is_file():
            match = _HEADER.search(path.read_text(encoding="utf-8", errors="ignore")[:800])
            if match:
                found.add(match.group(1))
    return found


def _pipeline_sources(root: Path) -> str:
    """Source of the asset bundler: ir_qweb.py (v8-v9), ir_qweb/assetsbundle.py
    (v10-v11), models/assetsbundle.py (v12+)."""
    base = next(p for p in (root / "openerp/addons/base", root / "odoo/addons/base")
                if p.is_dir())
    files = [*base.rglob("assetsbundle.py"), *base.glob("ir/ir_qweb.py")]
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_bootstrap_version_is_the_shipped_one(major):
    root = _checkout(major)
    assert _shipped_bootstrap_versions(root) == {_row(major)["bootstrap_version"]}


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_every_entry_point_exists(major):
    root = _checkout(major)
    missing = [p for p in _row(major)["scss_files"] if not (root / p).is_file()]
    assert not missing, missing


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_preprocessor_is_compiled_by_the_asset_pipeline(major):
    root = _checkout(major)
    row = _row(major)
    language = row["preprocessor"]
    assert re.search(_PIPELINE_MARKER[language], _pipeline_sources(root)), language
    own = [p for p in row["scss_files"] if "/static/lib/" not in p]
    assert any(p.endswith(_EXTENSION[language]) for p in own), (language, own)
