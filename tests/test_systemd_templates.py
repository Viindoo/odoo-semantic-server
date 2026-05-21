# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Systemd service file guard — unit tests (no Docker needed).

Asserts that every EnvironmentFile directive in systemd service files uses
the optional-file syntax (``EnvironmentFile=-/path``) so that a missing .env
file does not cause systemd to fail the unit with ``Result: resources`` and
trigger an infinite restart loop.

Background: the Web UI Python code gracefully warns when FERNET_KEY is absent
and continues running.  Requiring the .env file at the systemd level defeats
that graceful handling — 1146 restart loops were observed in production when
the file was absent on a fresh deployment.

Note: the ``systemd/`` directory with ``%i``/``%h`` instance-unit templates was
removed.  Those specifiers are only valid for *instance* units
(``foo@bar.service``), but ``install.sh --systemd`` deploys them as *regular*
units, causing ``systemd-analyze verify`` to report
``Invalid user/group name or numeric ID``.  All unit files are now under
``docs/deploy/`` (Variant A — production canonical).

Reference: systemd.exec(5) — prefix ``-`` means "ignore if file missing".
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Only docs/deploy/ is canonical now; systemd/ (Variant B) has been removed.
SERVICE_FILE_PATTERNS = [
    "docs/deploy/*.service",
]


def _collect_service_files():
    """Return all .service files tracked in the repo."""
    found = []
    for pattern in SERVICE_FILE_PATTERNS:
        found.extend(sorted(REPO_ROOT.glob(pattern)))
    return found


def test_environment_file_is_optional_in_all_templates():
    """Every EnvironmentFile= line must use the optional-file prefix ``-``.

    Systemd silently skips the file when the path is prefixed with ``-``.
    Without the prefix, a missing file causes the unit to enter failed state
    and restart in a tight loop.
    """
    service_files = _collect_service_files()
    assert service_files, (
        "No .service files found under docs/deploy/."
        " Check SERVICE_FILE_PATTERNS in this test if the directory layout changed."
    )

    violations = []
    for service_file in service_files:
        content = service_file.read_text()
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            # Must be an EnvironmentFile directive that is NOT already optional
            if stripped.startswith("EnvironmentFile=") and not stripped.startswith(
                "EnvironmentFile=-"
            ):
                violations.append(
                    f"  {service_file.relative_to(REPO_ROOT)}:{lineno}: {stripped}"
                )

    assert not violations, (
        "EnvironmentFile directives without the optional-file prefix '-' found:\n"
        + "\n".join(violations)
        + "\n\n"
        "Fix: change 'EnvironmentFile=/path' to 'EnvironmentFile=-/path'.\n"
        "Without the '-', systemd fails the unit when the file is absent,\n"
        "causing an infinite restart loop (Result: resources)."
    )


def test_service_files_are_present():
    """Sanity check: the expected canonical service files exist and are non-empty."""
    expected = [
        REPO_ROOT / "docs" / "deploy" / "odoo-semantic-mcp.service",
        REPO_ROOT / "docs" / "deploy" / "odoo-semantic-webui.service",
    ]
    for path in expected:
        assert path.exists(), f"Expected service file not found: {path}"
        assert path.stat().st_size > 0, f"Service file is empty: {path}"


# ---------------------------------------------------------------------------
# systemd-analyze verify regression test
# ---------------------------------------------------------------------------

# Fatal markers emitted by systemd-analyze when a unit has syntax or specifier
# errors (e.g. %i/%h in a regular unit).  These strings appear on stderr and
# indicate the unit *will not start* — they are distinct from non-fatal
# warnings about missing binaries or users which are expected on CI runners.
_FATAL_MARKERS = (
    "Unit configuration has fatal error",
    "has a bad unit file setting",
    "Invalid user/group name or numeric ID",
)


def test_service_files_pass_systemd_analyze_verify():
    """Service files in docs/deploy/ must have no fatal systemd syntax errors.

    Uses ``systemd-analyze verify`` to catch specifier bugs (e.g. ``%i``/``%h``
    in a regular unit) and other fatal configuration mistakes.  The test is
    skipped automatically when ``systemd-analyze`` is not available (e.g. macOS
    or minimal CI containers).

    Exit-code semantics: ``systemd-analyze verify`` exits non-zero when the
    target binary is missing or the user does not exist on the runner — those
    are expected on CI and are *not* treated as failures here.  Only the fatal
    marker strings in stderr indicate a real unit-file bug.
    """
    if not shutil.which("systemd-analyze"):
        import pytest

        pytest.skip("systemd-analyze not available on this runner")

    service_files = _collect_service_files()
    assert service_files, "No .service files found under docs/deploy/."

    fatal_findings = []
    for service_file in service_files:
        # Copy to an isolated temp dir so systemd-analyze does not pick up
        # stale files from /tmp/ or other locations via transitive dependency
        # resolution (it scans the directory of the verified file for peers).
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_copy = Path(tmpdir) / service_file.name
            tmp_copy.write_text(service_file.read_text())

            result = subprocess.run(
                ["systemd-analyze", "verify", str(tmp_copy)],
                capture_output=True,
                text=True,
            )
            combined = result.stdout + result.stderr
            for marker in _FATAL_MARKERS:
                if marker in combined:
                    fatal_findings.append(
                        f"  {service_file.relative_to(REPO_ROOT)}: "
                        f"systemd-analyze reports fatal error — {marker!r}\n"
                        f"  Full output:\n"
                        + "\n".join(f"    {ln}" for ln in combined.splitlines())
                    )
                    break  # one report per file is enough

    assert not fatal_findings, (
        "systemd-analyze verify found fatal errors in service files:\n\n"
        + "\n\n".join(fatal_findings)
        + "\n\n"
        "Common causes: %i/%h specifiers in a regular (non-instance) unit, "
        "malformed section headers, or unknown directives."
    )
