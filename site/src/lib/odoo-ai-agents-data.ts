// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * SSOT data module for the /odoo-ai-agents landing page.
 *
 * Exports typed data for all sections: agents, skill groups, commands,
 * personas, and FAQ entries. Content is English-only per D4 (DECISIONS.md).
 * No inline markup — consumers render as needed.
 *
 * Counts are imported from plugins-data.ts (SSOT) and MUST NOT be re-declared.
 * Skill uniqueness: odoo-test-writer appears in both Developer and QA/CS source
 * surveys; it is ONLY listed under QA/CS here so that `unique skills = 41`.
 *
 * Verify: `skillGroups.flatMap(g => g.skills)` filtered to distinct values === 41.
 */

export {
  SKILLS_COUNT,
  AGENTS_COUNT,
  COMMANDS_COUNT,
  WORKFLOWS_COUNT,
  PERSONAS_COUNT,
  PERSONA_COUNT,
} from './plugins-data';

// ---------------------------------------------------------------------------
// Type definitions
// ---------------------------------------------------------------------------

/** Model tier a specialist agent runs on by default. */
export type AgentTier = 'opus' | 'sonnet' | 'haiku' | 'fable';

/** One of the 7 specialist agents in the plugin. */
export interface Agent {
  /** Stable kebab-case identifier matching the agent file name. */
  id: string;
  /** Display name as shown in documentation and UI. */
  name: string;
  /** One-line role description (what this agent does, not when to call it). */
  role: string;
  /** Default model tier. Consumers may show this as a badge. */
  tier: AgentTier;
  /** Primary marketing benefit — one sentence, benefit-first. */
  benefit: string;
}

/** A group of skills sharing a persona and value proposition. */
export interface SkillGroup {
  /** Display label for this group (includes persona name). */
  name: string;
  /** Primary persona served by this group. */
  persona: string;
  /**
   * Skill identifiers in this group.
   * INVARIANT: no skill appears in more than one group across all groups —
   * this ensures `skillGroups.flatMap(g => g.skills)` gives 41 unique values.
   */
  skills: string[];
  /** One to two sentences explaining the value delivered to this persona. */
  valueProp: string;
}

/** One of the 9 workflow slash commands. */
export interface Command {
  /** Slash command name without the leading `/`. */
  name: string;
  /** What this command does in one or two sentences. */
  description: string;
}

/** One of the 9 personas the plugin serves. */
export interface Persona {
  /** Display name of this persona (job title or role). */
  name: string;
  /** One-sentence description of what this persona does with the plugin. */
  description: string;
  /** Whether this persona is the primary / developer persona for hero emphasis. */
  primary: boolean;
}

/** One FAQ entry — question and self-contained answer. */
export interface FaqEntry {
  /** The question text — must match the FAQPage JSON-LD exactly for rich results. */
  question: string;
  /**
   * Self-contained answer, 60-120 words.
   * Ends with a fact, not a CTA. No "click here" or "see above".
   */
  answer: string;
}

// ---------------------------------------------------------------------------
// 7 Agents
// ---------------------------------------------------------------------------

/**
 * The seven specialist agents shipped with the odoo-ai-agents plugin.
 * All are depth-1 leaf workers — they do not spawn sub-agents.
 * Order mirrors the typical pipeline: design -> code -> review -> debug -> UI.
 */
export const agents: Agent[] = [
  {
    id: 'odoo-solution-architect',
    name: 'odoo-solution-architect',
    role: 'Design-only leaf agent — produces a Technical Design Document before any code is written.',
    tier: 'opus',
    benefit:
      'Get an AI architect that designs the inheritance axis, override chain, and impact matrix before your developers touch a single file — grounded in the indexed codebase, not guesswork.',
  },
  {
    id: 'odoo-coder',
    name: 'odoo-coder',
    role: 'Backend write agent — produces production Python and XML Odoo code with ORM validation.',
    tier: 'sonnet',
    benefit:
      'Ship production Odoo Python on the first pass — every field verified against the index, every ORM chain validated, and a failing test written before the implementation.',
  },
  {
    id: 'odoo-frontend-coder',
    name: 'odoo-frontend-coder',
    role: 'Frontend write agent - covers OWL 2.x and legacy Widget eras across v8 to the latest version.',
    tier: 'sonnet',
    benefit:
      'OWL components, QWeb templates, and SCSS overrides that render on-theme on the target Odoo version — import paths and design tokens grounded in indexed source, not training memory.',
  },
  {
    id: 'odoo-code-reviewer',
    name: 'odoo-code-reviewer',
    role: 'Review leaf agent — produces severity-graded findings (CRITICAL/HIGH/MED/LOW) with corrected code.',
    tier: 'sonnet',
    benefit:
      'Every finding is evidence-backed with indexed output and line citation — not a generic linter, but an Odoo-aware review that catches the bugs that slip past conventional static analysis.',
  },
  {
    id: 'odoo-backend-debugger',
    name: 'odoo-backend-debugger',
    role: 'Diagnose-backend leaf agent — proves root cause via confirm-by-toggle, then hands off fix location.',
    tier: 'sonnet',
    benefit:
      'Proven root cause, not a plausible guess — the debugger cannot fill its Output Contract until it has actually toggled the cause on and off.',
  },
  {
    id: 'odoo-ui-debugger',
    name: 'odoo-ui-debugger',
    role: 'Diagnose-frontend leaf agent — correlates live browser evidence with indexed stylesheet chain.',
    tier: 'sonnet',
    benefit:
      'Diagnose a blank OWL screen or invisible CSS in minutes — the agent reads both live computed styles and the indexed stylesheet chain to pinpoint the exact file and selector, not just "clear cache and try again".',
  },
  {
    id: 'odoo-ui-reviewer',
    name: 'odoo-ui-reviewer',
    role: 'Rate-UI leaf agent — six-lens verdict: aesthetics, functional, stability, a11y, performance, design-system.',
    tier: 'sonnet',
    benefit:
      'A six-lens UI verdict with Lighthouse scores, accessibility findings, and design-token reality check — grounded in both the live rendered screen and the indexed stylesheet source, not a visual eyeball.',
  },
];

// ---------------------------------------------------------------------------
// 41 Skills in 7 groups
//
// DEDUPLICATION NOTE:
//   odoo-test-writer appears in both the Developer survey (phase4b Group 1)
//   and the QA/CS survey (phase4b Group 5). It is placed ONLY in the QA/CS
//   group here. This brings the Developer group to 12 skills and keeps the
//   total unique skill count at exactly 41.
//   Test: new Set(skillGroups.flatMap(g => g.skills)).size === 41
// ---------------------------------------------------------------------------

export const skillGroups: SkillGroup[] = [
  {
    name: 'Engineering & Developer Skills',
    persona: 'Odoo Developer / Tech Lead / Full-stack Engineer',
    skills: [
      'odoo-coding',
      'odoo-code-review',
      'odoo-debug',
      'odoo-solution-design',
      'odoo-override-finding',
      'odoo-perf-audit',
      'odoo-security-audit',
      'odoo-deprecation-audit',
      'odoo-data-migration',
      'odoo-version-diff',
      'odoo-frontend-design',
      'odoo-addon-diff',
    ],
    // 12 skills — odoo-test-writer is in QA/CS to avoid duplicate
    valueProp:
      'From writing a computed field to refactoring a multi-file module, developers describe the behavior they want and the AI analyzes the correct hook point, dispatches coder and frontend-coder in dependency order, and verifies every field name against the indexed source — wrong names are caught before they reach git.',
  },
  {
    name: 'Sales & Pre-sales Skills',
    persona: 'Sales AE / Pre-sales Consultant / Bid Manager',
    skills: [
      'odoo-gap-analysis',
      'odoo-brl',
      'odoo-rfp-response',
      'odoo-capability-proof',
      'odoo-feature-check',
      'odoo-discovery-summary',
      'odoo-deal-followup',
      'odoo-pricing-proposal',
    ],
    valueProp:
      'From discovery call to RFP response, skills automate artifact production with evidence drawn from the indexed source — AEs have proposal-ready materials prepared before the meeting, in minutes rather than hours.',
  },
  {
    name: 'Marketing & Content Skills',
    persona: 'Marketer / Product Manager / Content Creator',
    skills: [
      'odoo-content-draft',
      'odoo-feature-highlights',
      'odoo-campaign-plan',
      'odoo-competitive-brief',
      'odoo-objection-handling',
    ],
    valueProp:
      'From campaign blueprints to original copy, skills grounded on api_version_diff and check_module_exists ensure every claim about Odoo can be verified — no more marketing copy that contradicts indexed facts.',
  },
  {
    name: 'Consultant & Project Management Skills',
    persona: 'Odoo Consultant / Project Manager / Implementation Architect',
    skills: [
      'odoo-customization-inventory',
      'odoo-deploy-checklist',
      'odoo-risk-overview',
      'odoo-onboarding',
      'odoo-deep-survey',
    ],
    valueProp:
      'From onboarding a new customer to inventorying all customizations before M&A due diligence, consultants produce high-quality artifacts directly in the IDE without switching tools.',
  },
  {
    name: 'QA & Customer Success Skills',
    persona: 'QA Engineer / Customer Success Manager / Support',
    skills: [
      'odoo-qa-suite',
      'odoo-test-writer',
      'odoo-customer-health',
      'odoo-support-triage',
    ],
    valueProp:
      'QA gets a full QA suite (test cases, deploy checklist, bug triage) in one command; Customer Success gets a health score with churn signals and upsell opportunities after entering a customer profile.',
  },
  {
    name: 'Visual & UI Skills',
    persona: 'Developer / QA / Marketing / Demo Team',
    skills: [
      'odoo-ui-review',
      'odoo-visual-regression',
      'odoo-demo-recording',
    ],
    valueProp:
      'From screenshot baselines to live Lighthouse audits, teams verify that the UI looks "native Odoo" before demos or deployments — without manual browser testing.',
  },
  {
    name: 'Orchestration & Meta Skills',
    persona: 'All personas (auto-dispatch) / System Orchestrator',
    skills: [
      'odoo-intake',
      'wave',
      'workflow-chaining',
      'run-driver',
    ],
    valueProp:
      'odoo-intake is the universal front door that routes any plain-language intent to the right specialist; wave and workflow-chaining handle parallel git changes safely without touching the main branch; run-driver drives multi-step pipelines to completion.',
  },
];

// ---------------------------------------------------------------------------
// 9 Commands
// ---------------------------------------------------------------------------

export const commands: Command[] = [
  {
    name: 'odoo-respond-bid',
    description:
      'Generate a complete bid response package from raw prospect input through six gated phases: discovery synthesis, gap analysis, capability proof, objection pre-emption, and proposal draft.',
  },
  {
    name: 'odoo-draft-followup',
    description:
      'Draft a follow-up email for a stalled or at-risk deal, with explicit save-to-disk confirmation before any file is written — never auto-sends.',
  },
  {
    name: 'odoo-summarize-discovery',
    description:
      'Synthesize raw meeting or discovery notes into a structured customer profile covering business context, pain points, goals, and product fit.',
  },
  {
    name: 'odoo-position-feature',
    description:
      'Generate positioning copy for a specific Odoo feature across four phases: feature-check, addon-diff, competitive-brief, and final positioning copy for marketing assets or sales decks.',
  },
  {
    name: 'odoo-plan-upgrade',
    description:
      'Generate a comprehensive Odoo upgrade plan from source version to target version across four phases: risk overview, deprecation audit, version diff, and synthesis — with optional handoff to solution design.',
  },
  {
    name: 'odoo-setup',
    description:
      'One-shot idempotent setup: wire three browser MCP servers across Claude Code, Codex, and Gemini; install browser dependencies; auto-allow permissions; discover local Odoo instances.',
  },
  {
    name: 'odoo-run-brl',
    description:
      'Process a business requirement list (BRL) of any size into a four-way classified, costed, dependency-ordered plan with RTM export (rtm.csv, cost.json, dag.mermaid, report.md).',
  },
  {
    name: 'odoo-produce-video',
    description:
      'Produce a multi-scene Odoo demo video across three gated phases: storyboard, record each scene via odoo-demo-recording, and assemble into a single MP4 or GIF.',
  },
  {
    name: 'odoo-run-wave',
    description:
      'Kick off depth-0 multi-subagent git-wave orchestration: integration branch, per-WI worktrees, cherry-pick, end-of-wave Opus review, PR creation, squash with tree-identity gate, and human-confirm before merge.',
  },
];

// ---------------------------------------------------------------------------
// 9 Personas
// ---------------------------------------------------------------------------

export const personas: Persona[] = [
  {
    name: 'Engineer',
    description:
      'Finds correct override points, audits deprecated APIs before an upgrade, and validates deployments using odoo-override-finding, odoo-deprecation-audit, and odoo-deploy-checklist.',
    primary: false,
  },
  {
    name: 'Coder',
    description:
      'Writes idiomatic Odoo backend (Python/XML) or frontend (JS/OWL) code and debugs runtime failures using odoo-coding, odoo-debug, odoo-test-writer, odoo-security-audit, and odoo-perf-audit.',
    primary: true,
  },
  {
    name: 'Code Reviewer',
    description:
      'Reviews PRs or audits patches for ORM misuse, inheritance anti-patterns, security holes, and N+1 queries using odoo-code-review with specialist agents odoo-code-reviewer and odoo-backend-debugger.',
    primary: false,
  },
  {
    name: 'Visual / UI QA',
    description:
      'Reviews live Odoo screens across six lenses, debugs broken renders, catches visual regressions, records demos, and runs the full QA pipeline using odoo-ui-review, odoo-visual-regression, odoo-demo-recording, and odoo-qa-suite.',
    primary: false,
  },
  {
    name: 'Pre-Sales Consultant',
    description:
      'Verifies feature availability, builds gap matrices, produces evidence for proposals, and classifies and costs BRL requirements at scale using odoo-feature-check, odoo-gap-analysis, odoo-capability-proof, odoo-addon-diff, and odoo-brl.',
    primary: false,
  },
  {
    name: 'Sales AE',
    description:
      'Produces ACA-structured objection responses, risk-scored follow-up emails for stalled deals, prospect profile syntheses, and RFP compliance matrices using odoo-objection-handling, odoo-deal-followup, odoo-discovery-summary, and odoo-rfp-response.',
    primary: false,
  },
  {
    name: 'Marketer',
    description:
      'Creates content around Odoo features — blog posts, slide decks, social copy, and multi-channel campaign plans — in marketing-ready language using odoo-feature-highlights, odoo-content-draft, odoo-campaign-plan, and odoo-competitive-brief.',
    primary: false,
  },
  {
    name: 'Strategist / CEO',
    description:
      'Produces executive risk overviews of customizations, structured customization inventories, competitor capability snapshots, and customer health scores using odoo-risk-overview, odoo-customization-inventory, odoo-competitive-brief, and odoo-customer-health.',
    primary: false,
  },
  {
    name: 'Onboarding / Concierge',
    description:
      'Universal front door for every persona — bootstraps project context, routes any plain-language intent to the right specialist or workflow using odoo-intake, odoo-onboarding, run-driver, and workflow-chaining.',
    primary: false,
  },
];

// ---------------------------------------------------------------------------
// 7 FAQ entries
// Text must match the FAQPage JSON-LD in odoo-ai-agents.astro exactly.
// ---------------------------------------------------------------------------

export const faqs: FaqEntry[] = [
  {
    question: 'What is the Odoo AI Agent Team plugin?',
    answer:
      'Odoo AI Agent Team is a Claude Code plugin that provides 41 pre-built AI skills, 7 autonomous specialist agents, and 9 slash commands. Every agent is grounded on the Odoo Semantic MCP knowledge graph, which indexes 12,400+ models and 184,000+ fields across Odoo 8 to the latest version. Unlike ungrounded AI, agents verify field names and inheritance chains against the indexed source before suggesting code.',
  },
  {
    question: 'Which Odoo versions does the plugin support?',
    answer:
      'The plugin supports Odoo 8 through the latest version. Recent major versions are actively maintained with regular index updates. Versions v8 through v13 are available as legacy with no active index updates. The newest in-development release is indexed from its development branch and is not recommended for production use.',
  },
  {
    question: 'How do I install Odoo AI Agent Team for Claude Code?',
    answer:
      'Run two commands in your terminal: (1) claude plugin marketplace add Viindoo/claude-plugins --scope user (2) claude plugin install odoo-ai-agents@viindoo-plugins --scope user. Then inside Claude Code run /odoo-semantic-mcp:connect and enter your API key from odoo-semantic.viindoo.com. Total setup time: under 2 minutes.',
  },
  {
    question: 'Is there a free tier for Odoo AI Agent Team?',
    answer:
      'Yes. The free tier provides 30 MCP queries per day with no credit card required. Paid plans with higher daily limits are available - see the pricing page for current options. All plans include the full set of 41 skills, 7 agents, and 9 commands; the free tier is limited only by daily query count.',
  },
  {
    question: 'What is the difference between Odoo AI Agent Team and odoo-ls (language server)?',
    answer:
      'odoo-ls is an IDE language server for syntax checking and autocompletion in a single local Odoo checkout. Odoo AI Agent Team is a Claude Code plugin that runs 7 specialist AI agents grounded on a cross-version knowledge graph covering 12 Odoo versions. Both can be used together: odoo-ls checks syntax in your editor while Odoo AI Agent Team handles semantic questions — what breaks if I change this field, what is the override chain, is this ORM path valid across versions.',
  },
  {
    question: 'What AI tools does Odoo AI Agent Team work with?',
    answer:
      'Odoo AI Agent Team is optimized for Claude Code. The underlying OSM knowledge graph (Odoo Semantic MCP) works with any MCP-compatible tool including Cursor, Codex CLI, Gemini CLI, VS Code 1.99+, Windsurf, JetBrains AI Assistant, and Continue.dev. The 41 skills and 7 agents are Claude Code-specific; other tools access the 25 MCP tools directly.',
  },
  {
    question: 'How accurate is Odoo AI Agent Team compared to ungrounded AI?',
    answer:
      'In a benchmark of 40 real-world Odoo coding tasks tested in 2026-05 with Claude claude-sonnet-4-5, AI grounded with OSM answered correctly 95% of the time. Without OSM, the same model answered correctly 43% of the time. The 52-point gap is largest on inheritance chain traversal and field version questions, where ungrounded AI hallucination rates are highest.',
  },
];
