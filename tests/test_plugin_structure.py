"""Validate that dist/odoo-semantic-plugin/ matches its plugin.json manifest."""
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent / 'dist' / 'odoo-semantic-plugin'
MANIFEST = PLUGIN_ROOT / '.claude-plugin' / 'plugin.json'


def load_manifest():
    with open(MANIFEST) as f:
        return json.load(f)


def test_manifest_exists():
    assert MANIFEST.exists(), f'missing {MANIFEST}'


def test_no_version_field():
    m = load_manifest()
    assert 'version' not in m, (
        'plugin.json must not have a version field — SHA is the version identifier. '
        'Remove the version field so auto-pin updates reach users automatically.'
    )


def test_mcp_json_exists():
    assert (PLUGIN_ROOT / '.mcp.json').exists()


def test_skills_directory_referenced():
    m = load_manifest()
    skills_ref = m.get('skills')
    assert skills_ref, 'skills field missing from plugin.json'
    skills_dir = (PLUGIN_ROOT / skills_ref.lstrip('./')).resolve()
    assert skills_dir.is_dir(), f'skills directory not found: {skills_dir}'
    skill_files = list(skills_dir.rglob('SKILL.md'))
    assert len(skill_files) > 0, f'no SKILL.md files found under {skills_dir}'


def test_agents_exist():
    m = load_manifest()
    for agent_path in m.get('agents', []):
        full = (PLUGIN_ROOT / agent_path).resolve()
        assert full.exists(), f'agent file not found: {full}'


def test_commands_exist():
    m = load_manifest()
    for cmd_path in m.get('commands', []):
        full = (PLUGIN_ROOT / cmd_path).resolve()
        assert full.exists(), f'command file not found: {full}'


def test_mcp_servers_file_exists():
    m = load_manifest()
    mcp_ref = m.get('mcpServers')
    if mcp_ref and isinstance(mcp_ref, str):
        full = (PLUGIN_ROOT / mcp_ref).resolve()
        assert full.exists(), f'mcpServers file not found: {full}'


def test_readme_exists():
    assert (PLUGIN_ROOT / 'README.md').exists()
