# Plugin Release Guide

> **Audience:** Server admins / maintainers packaging and publishing the Claude Code plugin.

## Overview

The plugin source lives at `dist/odoo-semantic-plugin/` in this repo. When plugin files change and merge to `master`, the SHA is automatically pinned in `Viindoo/claude-plugins` via the `pin-sha.yml` workflow — no manual step needed.

---

## Step 1 — Verify plugin source

```bash
# Validate plugin structure locally (requires Claude Code CLI)
claude plugin validate dist/odoo-semantic-plugin/

# Check all skills load
find dist/odoo-semantic-plugin/skills -name "SKILL.md" | wc -l
# Should be 11

# Run plugin structure test
~/.venv/odoo-semantic-mcp/bin/python -m pytest tests/test_plugin_structure.py -v

# Run disambiguation test
~/.venv/odoo-semantic-mcp/bin/python -m pytest tests/test_skill_disambiguation.py -v
```

---

## Step 2 — Tag and create GitHub Release (optional)

Tags are for release visibility only — they do not affect how users receive updates (SHA is the version identifier).

```bash
# After PR merged to master:
git pull origin master
git tag v0.3.0 -m "<release notes>"
git push origin v0.3.0

# Create GitHub Release with plugin zip artifact (optional)
cd dist
zip -r odoo-semantic-plugin-v0.3.0.zip odoo-semantic-plugin/
gh release create v0.3.0 \
  --title "v0.3.0 — <milestone name>" \
  --notes "See CHANGELOG.md for details." \
  odoo-semantic-plugin-v0.3.0.zip
```

---

## Step 3 — SHA pin (automatic)

After your PR merges to `master`, the `pin-sha.yml` workflow triggers automatically if any file under `dist/odoo-semantic-plugin/` changed:

1. Reads the merged commit SHA
2. Opens a PR on `Viindoo/claude-plugins` updating `marketplace.json`
3. Enables auto-merge — the PR merges once `validate` CI passes

**You do not need to do anything.** Monitor the workflow run at:
`Actions → Pin SHA in claude-plugins`

If the workflow fails, fall back to the manual process below.

---

## Step 3 (fallback) — Manual SHA pin

```bash
# Get the SHA of the merge commit
SHA=$(git rev-parse master)
echo "SHA: $SHA"

# Update viindoo/claude-plugins marketplace.json
git clone https://github.com/Viindoo/claude-plugins.git /tmp/claude-plugins-update
cd /tmp/claude-plugins-update

# Edit .claude-plugin/marketplace.json — update sha field to $SHA
# Then commit and push a PR:
git checkout -b "pin/odoo-semantic-${SHA:0:7}"
git add .claude-plugin/marketplace.json
git commit -m "pin odoo-semantic to ${SHA:0:7}"
git push origin HEAD
gh pr create --title "pin odoo-semantic to ${SHA:0:7}" --body "Manual pin."
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
   - **Do not set `version` in `plugin.json`** — SHA is the version identifier
2. Open a PR on `Viindoo/claude-plugins` adding an entry to `.claude-plugin/marketplace.json`:

```json
{
  "name": "your-plugin-name",
  "source": {
    "source": "git-subdir",
    "url": "https://github.com/Viindoo/<project-repo>.git",
    "path": "dist/<plugin-name>",
    "ref": "master",
    "sha": "<exact-commit-sha-after-merge>"
  },
  "description": "One-line description"
}
```

3. Set up `pin-sha.yml` in the new plugin repo following the template in [CONTRIBUTING.md](https://github.com/Viindoo/claude-plugins/blob/master/CONTRIBUTING.md)

---

## Anti-drift mechanism

`Viindoo/claude-plugins` runs `.github/workflows/validate.yml` nightly:
- Validates `marketplace.json` schema
- Checks each `git-subdir` source URL + ref is reachable
- Fails PR if source becomes unreachable (renamed repo, deleted branch)

If CI fails: update the `ref` or `sha` in `marketplace.json` to point to the current valid state.
