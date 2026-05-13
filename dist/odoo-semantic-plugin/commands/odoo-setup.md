# /odoo-semantic:setup

Interactive setup for the Odoo Semantic MCP plugin.

Run this **after** `claude plugin install` because Claude Code v2.1.x has a known
bug where `userConfig` values are never prompted at install time
(github.com/anthropics/claude-code/issues/39455). Without this step the plugin's
`.mcp.json` template cannot resolve and the `odoo-semantic` MCP server silently
fails to load.

## Steps for the AI agent

1. Ask user for MCP server URL. Default: `https://odoo-semantic.viindoo.com:9999/mcp`.
   Self-host alternative: `http://127.0.0.1:8002/mcp`.
2. Ask user to paste their API key. Must match `^osm_[A-Za-z0-9_-]+$`.
   Re-prompt on format mismatch. **Do not echo the key back in plain text.**
3. Run the following with the `Bash` tool (substitute `<URL>` and `<KEY>`):
   ```
   claude mcp add --scope user --transport http odoo-semantic <URL> \
     --header "X-API-Key: <KEY>"
   ```
   If `claude mcp add` reports the name already exists, run
   `claude mcp remove odoo-semantic --scope user` first, then retry.
   If exit code is non-zero for any other reason, report stderr to the user and stop.
4. Verify connectivity by calling MCP tool `resolve_model` with
   `model="res.partner"` and `version="17.0"`.
   - On success: print `✓ Odoo Semantic MCP ready. 14 tools active.`
   - On failure: ask the user to check VPN/firewall, confirm API key is active
     on the server, and re-run `/odoo-semantic:setup`.
5. List the 15 skill names shipped by this plugin so the user knows what to invoke.

## Hard rules

- Use the `Bash` tool for step 3. Do **not** use `Edit` or `Write` on
  `~/.claude.json` — that file holds unrelated user state (projects, startup
  counters, other MCP servers) and direct edits risk corruption.
- The API key is sensitive. Mask it (`osm_****`) in any output to the user.
- If the user already has an `odoo-semantic` MCP server registered in a higher
  scope, the plugin's `.mcp.json` template version is suppressed by Claude Code
  dedup rules — this is expected and not an error.
