"""
Systemd template guard — unit tests (no Docker needed).

Asserts that every EnvironmentFile directive in systemd service templates uses
the optional-file syntax (``EnvironmentFile=-/path``) so that a missing .env
file does not cause systemd to fail the unit with ``Result: resources`` and
trigger an infinite restart loop.

Background: the Web UI Python code gracefully warns when FERNET_KEY is absent
and continues running.  Requiring the .env file at the systemd level defeats
that graceful handling — 1146 restart loops were observed in production when
the file was absent on a fresh deployment.

Reference: systemd.exec(5) — prefix ``-`` means "ignore if file missing".
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Directories that contain service unit files or templates shipped in the repo.
# Only repo-tracked files are tested; /etc/systemd/ is managed by the operator.
SERVICE_FILE_PATTERNS = [
    "docs/deploy/*.service",
    "systemd/*.service",
    "systemd/*.service.template",
]


def _collect_service_files():
    """Return all .service / .service.template files tracked in the repo."""
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
        "No .service or .service.template files found under docs/deploy/ or systemd/."
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
    """Sanity check: the expected template files exist and are non-empty."""
    expected = [
        REPO_ROOT / "systemd" / "odoo-semantic-mcp.service.template",
        REPO_ROOT / "systemd" / "odoo-semantic-webui.service.template",
        REPO_ROOT / "docs" / "deploy" / "odoo-semantic-webui.service",
    ]
    for path in expected:
        assert path.exists(), f"Expected service file not found: {path}"
        assert path.stat().st_size > 0, f"Service file is empty: {path}"
