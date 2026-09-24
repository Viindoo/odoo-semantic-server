# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner version rule (08-approved-plan decision 4; OSM #378 node B4, test T15).

- A module's Odoo version ALWAYS comes from the standard branch name
  (``17.0``, ``17.1``, ``18.0``...; ``saas-17.1`` normalizes to ``17.1``).
- Manifest ``version`` short form (``1.0.0``) never decides anything.
- Long form (``16.0.1.0.0``) whose prefix disagrees with the branch keeps the
  BRANCH version, sets ``version_mismatch`` and keeps ``version_raw``.
- A non-standard branch falls back to the profile version, with attention.
- A standard branch that differs from the profile version keeps the branch
  version, with attention.
- ``X.1`` minors flow through VersionRegistry, era dispatch and numeric sort.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Module import (not name import) so that the pre-existing APIs stay reachable
# when the B4 patch is reverted for the revert proof.
from src.indexer import registry as reg
from src.indexer.registry import build_registry, get_manifest_finder, resolve_odoo_version
from src.indexer.version_registry import VersionRegistry


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    ).stdout


def _repo(path: Path, branch: str, modules: dict[str, dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    for name, spec in modules.items():
        d = path / name
        d.mkdir()
        (d / "__manifest__.py").write_text(
            f"{{'name': {name!r}, 'version': {spec.get('version', '1.0.0')!r}, "
            f"'depends': [], 'installable': {spec.get('installable', True)!r}, "
            f"'license': 'LGPL-3'}}\n"
        )
        (d / "__init__.py").write_text("")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c")
    return path


# --------------------------------------------------------------------------
# normalize_branch_version
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("branch", "expected"), [
    ("17.0", "17.0"),
    ("8.0", "8.0"),
    ("17.1", "17.1"),
    ("saas-17.1", "17.1"),
    ("refs/heads/18.0", "18.0"),
    ("main", None),
    ("master", None),
    ("17.0-fix", None),
    ("feature/17.0", None),
    ("", None),
    (None, None),
])
def test_branch_name_normalizes_to_an_odoo_version_only_when_standard(branch, expected):
    assert reg.normalize_branch_version(branch) == expected


# --------------------------------------------------------------------------
# manifest_version_mismatch
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "odoo_version", "expected"), [
    ("16.0.1.0.0", "17.0", True),
    ("18.0.1.0.0", "19.0", True),       # real: tvtmaaddons19 viin_*_suite
    ("17.0.1.0.0", "17.0", False),
    ("17.0.1.0", "17.0", False),
    ("17.0.1.0", "16.0", True),          # 4 numeric parts is long form
    ("1.0.0", "17.0", False),            # short form never mismatches
    ("1.0", "17.0", False),
    ("16.0", "17.0", False),             # 2 parts: not long form
    ("", "17.0", False),
])
def test_only_a_long_form_version_with_a_foreign_prefix_mismatches(raw, odoo_version, expected):
    assert reg.manifest_version_mismatch(raw, odoo_version) is expected


# --------------------------------------------------------------------------
# resolve_repo_version
# --------------------------------------------------------------------------

def test_standard_branch_is_the_version_without_attention(tmp_path):
    rv = reg.resolve_repo_version(str(tmp_path), branch="17.0", profile_version="17.0")
    assert rv.odoo_version == "17.0"
    assert rv.branch_version == "17.0"
    assert rv.attention == ()


def test_saas_branch_is_its_minor_version(tmp_path):
    rv = reg.resolve_repo_version(str(tmp_path), branch="saas-17.1", profile_version="17.1")
    assert rv.odoo_version == "17.1"
    assert rv.attention == ()


def test_branch_disagreeing_with_profile_keeps_branch_and_raises_attention(tmp_path):
    rv = reg.resolve_repo_version(str(tmp_path), branch="17.0", profile_version="16.0")
    assert rv.odoo_version == "17.0"
    assert len(rv.attention) == 1
    assert "17.0" in rv.attention[0] and "16.0" in rv.attention[0]
    assert rv.attention[0].isascii()


@pytest.mark.parametrize("branch", ["main", "17.0-hotfix", None])
def test_non_standard_branch_falls_back_to_profile_with_attention(tmp_path, branch):
    """``None`` = no registered branch and no git checkout: rendered '(none)'."""
    rv = reg.resolve_repo_version(str(tmp_path), branch=branch, profile_version="17.0")
    assert rv.odoo_version == "17.0"
    assert rv.branch_version is None
    assert len(rv.attention) == 1
    assert rv.attention[0].isascii()


@pytest.mark.parametrize("profile", [None, "", "unknown"])
def test_no_branch_and_no_profile_places_nothing_and_raises_no_attention(tmp_path, profile):
    rv = reg.resolve_repo_version(str(tmp_path), branch="main", profile_version=profile)
    assert rv.odoo_version is None
    assert rv.attention == ()


def test_branch_defaults_to_the_checked_out_branch(tmp_path):
    repo = _repo(tmp_path / "r", "16.0", {"a": {}})
    rv = reg.resolve_repo_version(str(repo), profile_version="16.0")
    assert (rv.branch, rv.odoo_version, rv.attention) == ("16.0", "16.0", ())


# --------------------------------------------------------------------------
# resolve_odoo_version (pre-existing public helper, new priority)
# --------------------------------------------------------------------------

def test_long_form_prefix_does_not_override_the_branch(tmp_path):
    """T15: ``16.0.1.0.0`` on a 17.0 branch is keyed 17.0 (before: 16.0)."""
    repo = _repo(tmp_path / "r", "17.0", {"a": {}})
    assert resolve_odoo_version("16.0.1.0.0", str(repo)) == "17.0"


def test_short_form_takes_the_branch_version(tmp_path):  # GUARD: pre-existing behaviour
    repo = _repo(tmp_path / "r", "16.0", {"a": {}})
    assert resolve_odoo_version("1.0.0", str(repo)) == "16.0"


def test_profile_beats_manifest_prefix_when_the_branch_is_not_standard(tmp_path):
    assert resolve_odoo_version(
        "16.0.1.0.0", str(tmp_path), branch="main", profile_version="17.0",
    ) == "17.0"


# GUARD: pre-existing behaviour
def test_long_form_prefix_places_only_when_nothing_else_exists(tmp_path):
    # Non-git dir, no profile: legacy callers still get the manifest prefix.
    assert resolve_odoo_version("15.0.1.0.0", str(tmp_path)) == "15.0"
    assert resolve_odoo_version("1.0.0", str(tmp_path)) == "unknown"


# --------------------------------------------------------------------------
# End to end through the scan and build_registry
# --------------------------------------------------------------------------

def test_long_form_mismatch_is_keyed_by_branch_and_flagged(tmp_path):
    """T15 end to end: branch 17.0, module shipping ``16.0.1.0.0`` -> indexed
    at 17.0 with version_mismatch and the raw string kept; a short version on
    the same branch is keyed 17.0 without a flag."""
    repo = _repo(tmp_path / "r", "17.0", {
        "carried": {"version": "16.0.1.0.0"},
        "short": {"version": "1.0.0"},
    })
    registry = build_registry([(str(repo), "17.0")])
    assert set(registry) == {"17.0"}
    carried = registry["17.0"]["carried"]
    assert carried.version_raw == "16.0.1.0.0"
    assert carried.version_mismatch is True
    assert registry["17.0"]["short"].version_mismatch is False


def test_excluded_module_also_carries_the_mismatch_flag(tmp_path):
    """Real shape: tvtmaaddons19 viin_*_suite at ``18.0.1.0.0``,
    installable False, on branch 19.0."""
    repo = _repo(tmp_path / "r", "19.0", {
        "viin_x_suite": {"version": "18.0.1.0.0", "installable": False},
    })
    scan = reg.build_registry_scan(str(repo), "19.0")
    ex = scan.excluded["viin_x_suite"]
    assert ex.version_mismatch is True
    assert ex.version_raw == "18.0.1.0.0"


def test_saas_branch_indexes_modules_at_the_minor_version(tmp_path):
    """The branch alone places the modules: no profile version, and a short
    manifest version, so ``17.1`` can only come from ``saas-17.1``."""
    repo = _repo(tmp_path / "r", "saas-17.1", {"a": {"version": "1.0.0"}})
    via_registry = build_registry([(str(repo), "")])
    assert set(via_registry) == {"17.1"}
    scan = reg.build_registry_scan(str(repo))
    assert scan.odoo_version == "17.1"
    assert set(scan.modules) == {"17.1"}
    assert scan.module("a").version_mismatch is False
    assert scan.attention == []


def test_non_standard_branch_scan_uses_profile_version_with_attention(tmp_path):
    repo = _repo(tmp_path / "r", "main", {"a": {"version": "16.0.1.0.0"}})
    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.odoo_version == "17.0"
    assert set(scan.modules) == {"17.0"}
    assert scan.module("a").version_mismatch is True
    assert len(scan.attention) >= 1


def test_registered_branch_wins_over_the_checked_out_one(tmp_path):
    """``branch=`` is the registered ``repos.branch``: the scan keys by it."""
    repo = _repo(tmp_path / "r", "16.0", {"a": {}})
    registry = build_registry([(str(repo), "17.0")], branch="17.0")
    assert set(registry) == {"17.0"}


def test_branch_vs_profile_disagreement_reaches_the_scan_attention(tmp_path):
    repo = _repo(tmp_path / "r", "17.0", {"a": {}})
    scan = reg.build_registry_scan(str(repo), "16.0")
    assert scan.odoo_version == "17.0"
    assert set(scan.modules) == {"17.0"}
    assert any("16.0" in a for a in scan.attention)


# --------------------------------------------------------------------------
# X.1 minors through version dispatch and sort (GUARD: pre-existing behaviour)
# --------------------------------------------------------------------------

def test_minor_versions_dispatch_like_their_major():  # GUARD: pre-existing behaviour
    reg = VersionRegistry([(8, 9, "era1"), (10, None, "era2")])
    assert reg.resolve_version("17.1") == reg.resolve_version("17.0") == "era2"
    assert reg.resolve_version("9.1") == "era1"
    assert type(get_manifest_finder("17.1")) is type(get_manifest_finder("17.0"))
    assert type(get_manifest_finder("9.1")) is type(get_manifest_finder("9.0"))


def test_previous_indexed_version_sorts_minors_numerically():  # GUARD: pre-existing behaviour
    from src.indexer.pipeline import _find_previous_indexed_version

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def data(self):
            return self._rows

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, *_a, **_k):
            return _Result([{"v": v} for v in ("9.0", "16.0", "17.0", "17.1", "18.0")])

    class _Driver:
        def session(self):
            return _Session()

    class _Writer:
        driver = _Driver()

    assert _find_previous_indexed_version("18.0", _Writer()) == "17.1"
    assert _find_previous_indexed_version("17.1", _Writer()) == "17.0"
    assert _find_previous_indexed_version("10.0", _Writer()) == "9.0"
