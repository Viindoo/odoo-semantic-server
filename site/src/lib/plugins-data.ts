// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT for 2 Claude Code plugins (MIT, free) — slug, install command, value props.
 *
 *  Used by Astro components: InstallSnippets.astro (Claude Code tab) +
 *  OpenSourcePlugins.astro (promo section). Static HTML
 *  (`src/mcp/static/install/index.html`) and README/docs cannot import this —
 *  must sync manually, with note "sync from plugins-data.ts".
 *
 *  Source of truth for slug/version/dependency: client repo
 *  github.com/Viindoo/odoo-mcp-client (split commit 806a159, 2026-05-29). */

export const PLUGIN_MARKETPLACE = 'Viindoo/claude-plugins';
export const PLUGIN_MARKETPLACE_ALIAS = 'viindoo-plugins';
export const PLUGIN_REPO_URL = 'https://github.com/Viindoo/odoo-mcp-client';

/** Command users run after install to wire URL + API key. */
export const CONNECT_COMMAND = '/odoo-semantic-mcp:connect';

/** Plugin counts — source: plugin-survey.md (odoo-mcp-client VERSION 2.1.0). */
export const SKILLS_COUNT = 26;
export const AGENTS_COUNT = 3;
export const COMMANDS_COUNT = 6;
export const PERSONA_COUNT = 9;

export interface PluginMeta {
  slug: string;
  version: string;
  /** Short one-line value pitch. */
  tagline: string;
  /** Bullet value props (benefit-first, marketing voice). */
  highlights: string[];
}

export const MCP_PLUGIN: PluginMeta = {
  slug: 'odoo-semantic-mcp',
  version: '1.0.0',
  tagline: 'One-command connect to the MCP server.',
  highlights: [
    `${CONNECT_COMMAND} prompts for URL + API key, then auto-allows every mcp__odoo-semantic__* tool`,
    'Wires the HTTP MCP config so all 24 tools + 7 resources light up in one step',
  ],
};

export const SKILLS_PLUGIN: PluginMeta = {
  slug: 'odoo-semantic-skills',
  version: '2.1.0',
  tagline: `${SKILLS_COUNT} skills · ${AGENTS_COUNT} agents · ${PERSONA_COUNT} personas — auto-pulls the MCP plugin.`,
  highlights: [
    `${SKILLS_COUNT} skills fire from plain-English intent — no tool names to memorize`,
    `${AGENTS_COUNT} agents: odoo-coder, odoo-code-reviewer, odoo-ui-reviewer — review/code/UI grounded in the indexed graph`,
    `${PERSONA_COUNT} personas for dev · consultant · CEO · sales · marketer · visual QA`,
  ],
};

/** Install command lines (slash-command form for in-session Claude Code).
 *
 *  PRIMARY install path  → marketplace + installMcp + connect
 *  OPTIONAL add-on       → installSkills (26 skills · 3 agents · 9 personas; auto-pulls MCP)
 */
export const INSTALL_STEPS_SLASH = {
  marketplace: `/plugin marketplace add ${PLUGIN_MARKETPLACE}`,
  /** PRIMARY — install the MCP plugin. */
  installMcp: `/plugin install ${MCP_PLUGIN.slug}@${PLUGIN_MARKETPLACE_ALIAS}`,
  /** Alias kept for back-compat. */
  installMcpOnly: `/plugin install ${MCP_PLUGIN.slug}@${PLUGIN_MARKETPLACE_ALIAS}`,
  /** OPTIONAL add-on — adds skills/agents/personas; also auto-pulls MCP if not yet installed. */
  installSkills: `/plugin install ${SKILLS_PLUGIN.slug}@${PLUGIN_MARKETPLACE_ALIAS}`,
  connect: CONNECT_COMMAND,
} as const;

/** Install command lines (CLI form — `claude plugin ...`).
 *
 *  PRIMARY install path  → marketplace + installMcp (= installMcpOnly alias) + connect
 *  OPTIONAL add-on       → installSkills (26 skills · 3 agents · 9 personas; auto-pulls MCP)
 */
export const INSTALL_STEPS_CLI = {
  marketplace: `claude plugin marketplace add ${PLUGIN_MARKETPLACE} --scope user`,
  /** PRIMARY — install the MCP plugin (24 tools + 7 resources, no skills). */
  installMcp: `claude plugin install ${MCP_PLUGIN.slug}@${PLUGIN_MARKETPLACE_ALIAS} --scope user`,
  /** Alias kept for back-compat with existing component references. */
  installMcpOnly: `claude plugin install ${MCP_PLUGIN.slug}@${PLUGIN_MARKETPLACE_ALIAS} --scope user`,
  /** OPTIONAL add-on — adds skills/agents/personas; also auto-pulls MCP if not yet installed. */
  installSkills: `claude plugin install ${SKILLS_PLUGIN.slug}@${PLUGIN_MARKETPLACE_ALIAS} --scope user`,
  connect: CONNECT_COMMAND,
} as const;
