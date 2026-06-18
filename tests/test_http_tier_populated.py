# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: the `http` marker tier must never be silently empty.

Business rule: `make test-http` runs `pytest -m http` and must collect at
least one test.  If every `pytestmark = pytest.mark.http` is removed (e.g.
accidental strip during a refactor), `make test-http` would silently exit-5
(no tests collected) — passing CI while the tier vanishes.

This guard prevents that by statically counting test files that declare the
`http` marker.  It intentionally excludes itself and the marker-discipline
file so only the real tagged-test files are counted.

Red-green proof: remove all `pytestmark = pytest.mark.http` lines from the
10 in-process test files and this test turns RED — asserting 0 >= 1 fails.
"""
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve()
_EXCLUDE = {_SELF, _TESTS_DIR / "test_marker_discipline.py"}


def test_http_tier_is_not_empty():
    """At least one test file must carry `pytestmark = pytest.mark.http`.

    If this test turns RED it means every http-tagged file lost its marker —
    run `grep -rn 'pytest.mark.http' tests/` to find what's missing.
    """
    tagged = [
        f
        for f in _TESTS_DIR.rglob("test_*.py")
        if f.resolve() not in _EXCLUDE
        and "pytest.mark.http" in f.read_text(encoding="utf-8")
    ]
    assert len(tagged) >= 1, (
        "No test files carry `pytest.mark.http` — the http tier is empty. "
        "`make test-http` would exit-5 (no tests collected). "
        f"Add `pytestmark = pytest.mark.http` to the relevant in-process "
        f"FastAPI test files in {_TESTS_DIR}."
    )
