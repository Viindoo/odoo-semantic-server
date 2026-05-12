# Plugin Release Guide

> **Audience:** Server admins / maintainers packaging and publishing the Claude Code plugin.

## Overview

The plugin source lives at `dist/odoo-semantic-plugin/` in this repo. Each release:
1. Tags a git commit in `odoo-semantic-mcp`
2. Creates a GitHub Release with a zip artifact
3. Updates `Viindoo/claude-plugins` marketplace.json with the new SHA

---

## Step 1 — Verify plugin source

```bash
# Validate plugin structure locally (requires Claude Code CLI)
claude plugin validate dist/odoo-semantic-plugin/

# Check all skills load
find dist/odoo-semantic-plugin/skills -name "SKILL.md" | wc -l
# Should be 11

# Run disambiguation test
~/.venv/odoo-semantic-mcp/bin/python -m pytest tests/test_skill_disambiguation.py -v
```

---

## Step 2 — Tag and create GitHub Release

```bash
# After PR merged to main:
git pull origin main
git tag v0.2.0 -m "M7.5 Persona Wow: TRIGGER docstrings + Claude Code plugin + cross-vendor adapters"
git push origin v0.2.0

# Create GitHub Release with plugin zip artifact
cd dist
zip -r odoo-semantic-plugin-v0.2.0.zip odoo-semantic-plugin/
gh release create v0.2.0 \
  --title "v0.2.0 — M7.5 Persona Wow" \
  --notes "See CHANGELOG.md for details." \
  odoo-semantic-plugin-v0.2.0.zip
```

---

## Step 3 — Pin SHA in Viindoo/claude-plugins

After the release tag is pushed, pin the exact commit SHA in the marketplace:

```bash
# Get the SHA of the release tag
SHA=$(git rev-parse v0.2.0)
echo "SHA: $SHA"

# Update viindoo/claude-plugins marketplace.json
git clone https://github.com/Viindoo/claude-plugins.git /tmp/claude-plugins-update
cd /tmp/claude-plugins-update

# Edit .claude-plugin/marketplace.json — add sha field to odoo-semantic entry:
# "sha": "<SHA from above>"
# Then commit and push:
git add .claude-plugin/marketplace.json
git commit -m "pin odoo-semantic to v0.2.0 ($SHA)"
git push origin master
```

---

## Step 4 — Validate marketplace update

The nightly CI in `Viindoo/claude-plugins` auto-validates plugin sources. You can also trigger it manually:

```bash
gh workflow run validate.yml --repo Viindoo/claude-plugins
gh run watch --repo Viindoo/claude-plugins
```

---

## Adding a new plugin (from another Viindoo project)

When a new Viindoo project ships a Claude Code plugin:

1. The plugin source lives in `<project-repo>/dist/<plugin-name>/` with `.claude-plugin/plugin.json`
2. Open a PR on `Viindoo/claude-plugins` adding an entry to `.claude-plugin/marketplace.json`:

```json
{
  "name": "your-plugin-name",
  "source": {
    "source": "git-subdir",
    "url": "https://github.com/Viindoo/<project-repo>.git",
    "path": "dist/<plugin-name>",
    "ref": "main",
    "sha": "<exact-commit-sha-after-merge>"
  },
  "description": "One-line description"
}
```

3. After PR merges, nightly CI validates the source remains reachable.

---

## Anti-drift mechanism

`Viindoo/claude-plugins` runs `.github/workflows/validate.yml` nightly:
- Validates `marketplace.json` schema
- Checks each `git-subdir` source URL + ref is reachable
- Fails PR if source becomes unreachable (renamed repo, deleted branch)

If CI fails: update the `ref` or `sha` in `marketplace.json` to point to the current valid state.
