// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT for the /examples showcase (and the landing teaser).
 *  Each scenario is a real Odoo question rendered as a before/after:
 *  what an ungrounded AI hallucinates vs. what Odoo Semantic MCP returns,
 *  grounded in the indexed knowledge graph. Token counts are illustrative of
 *  the compact, structured tool output (English-only per the API contract).
 *
 *  Keep this English-only — it mirrors the MCP tool surface. */

export interface CodeLine {
  text: string;
  /** Optional Tailwind class for this line (e.g. error red, success green). */
  cls?: string;
}

export interface ExampleScenario {
  /** Stable anchor id (used for deep links: /examples#<id>). */
  id: string;
  /** Short audience tag shown as a chip. */
  persona: string;
  /** The MCP tool this scenario exercises. */
  tool: string;
  /** The natural-language question a developer would ask their AI. */
  prompt: string;
  /** One-line framing of why the ungrounded answer hurts. */
  problem: string;
  without: { badge: string; lines: CodeLine[] };
  with: { badge: string; lines: CodeLine[]; tokens: string };
}

const DIM = 'text-viindoo-on-dark-dim';
const BAD = 'text-red-400';
const BAD_STRIKE = 'text-red-400 line-through';
const HEAD = 'text-viindoo-primary-bright font-semibold';
const OK = 'text-viindoo-success';

export const SCENARIOS: ExampleScenario[] = [
  {
    id: 'extending-modules',
    persona: 'Developer',
    tool: 'model_inspect',
    prompt: 'List all modules extending sale.order in Odoo 17',
    problem: 'Half the module names are invented or mixed across versions.',
    without: {
      badge: 'HALLUCINATED',
      lines: [
        { text: '// AI guessing from training data', cls: DIM },
        { text: 'In Odoo 17, sale.order is typically extended by:' },
        { text: "  • sale_advance_payment  ⚠ module doesn't exist", cls: BAD_STRIKE },
        { text: '  • sale_management' },
        { text: '  • sale_workflow  ⚠ removed in v15', cls: BAD_STRIKE },
        { text: '  • sale_stock' },
        { text: '  • sale_crm  ⚠ no such module', cls: BAD_STRIKE },
        { text: '  • sale_loyalty' },
        { text: '' },
        { text: '// 3 of 6 modules are fabricated', cls: DIM },
        { text: '// versions mixed: v13 / v15 / v17', cls: DIM },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      tokens: '418 tokens',
      lines: [
        { text: '// → model_inspect(sale.order, v=17.0)', cls: DIM },
        { text: 'sale.order [DEFINITION]', cls: HEAD },
        { text: '├─ defined_in: sale' },
        { text: '├─ total_fields: 148' },
        { text: '├─ total_methods: 62' },
        { text: '└─ extended_by:' },
        { text: '   ├─ sale_management   // +6 fields, +3 methods' },
        { text: '   ├─ sale_stock        // +12 fields, +7 methods' },
        { text: '   ├─ sale_loyalty      // +4 fields, +2 methods' },
        { text: '   ├─ sale_subscription // EE-only, +18 fields' },
        { text: '   └─ sale_purchase     // +3 fields, +5 methods' },
        { text: '' },
        { text: '✓ 5 modules · all verified against the knowledge graph index', cls: OK },
      ],
    },
  },
  {
    id: 'override-point',
    persona: 'Developer',
    tool: 'find_override_point',
    prompt: 'Where do I override action_confirm on sale.order without breaking the chain?',
    problem: 'Ungrounded answers skip super() ordering and miss real hook sites.',
    without: {
      badge: 'RISKY',
      lines: [
        { text: '// AI without the override chain', cls: DIM },
        { text: 'You can override action_confirm in your module:' },
        { text: '  def action_confirm(self):' },
        { text: '      res = super().action_confirm()' },
        { text: '      # ... your logic' },
        { text: '      return res' },
        { text: '' },
        { text: '// which modules already override it? unclear', cls: BAD },
        { text: '// safe ordering vs sale_stock? unknown', cls: BAD },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      tokens: '312 tokens',
      lines: [
        { text: '// → find_override_point(sale.order, action_confirm)', cls: DIM },
        { text: 'action_confirm() · override chain', cls: HEAD },
        { text: '├─ sale            // base definition' },
        { text: '├─ sale_stock      // delivery creation · calls super() first' },
        { text: '├─ sale_loyalty    // reward lines · calls super() last' },
        { text: '└─ sale_subscription // EE · wraps in savepoint' },
        { text: '' },
        { text: 'recommended hook: after sale_stock, before reward calc' },
        { text: '✓ 4 hooks · super() ordering resolved', cls: OK },
      ],
    },
  },
  {
    id: 'impact-analysis',
    persona: 'Developer · PM',
    tool: 'impact_analysis',
    prompt: 'What breaks if I change sale.order.amount_total semantics?',
    problem: 'Vague blast-radius answers force a manual grep across the codebase.',
    without: {
      badge: 'INCOMPLETE',
      lines: [
        { text: '// AI without a dependency graph', cls: DIM },
        { text: 'amount_total is a computed field aggregating' },
        { text: 'order_line.price_subtotal. Changing it may affect:' },
        { text: '  - reporting queries (which ones? unclear)', cls: BAD },
        { text: '  - invoicing logic (where exactly?)', cls: BAD },
        { text: '  - maybe accounting integration', cls: BAD },
        { text: '' },
        { text: '// no concrete file refs · agent must grep', cls: DIM },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      tokens: '3,218 tokens',
      lines: [
        { text: '// → impact_analysis(sale.order, amount_total)', cls: DIM },
        { text: 'BLAST RADIUS · sale.order.amount_total', cls: HEAD },
        { text: '├─ modules affected: 12' },
        { text: '├─ method overrides: 38' },
        { text: '├─ view references: 49' },
        { text: '├─ report references: 14' },
        { text: '└─ key dependents:' },
        { text: '   ├─ account.move        (3 hooks)' },
        { text: '   ├─ sale_loyalty        (1 hook)' },
        { text: '   └─ sale_subscription   (2 hooks)' },
        { text: '' },
        { text: '✓ full chain · every ref has file:line', cls: OK },
      ],
    },
  },
  {
    id: 'deprecation-scan',
    persona: 'Developer · Upgrade',
    tool: 'find_deprecated_usage',
    prompt: 'Is this v13 module code safe to run on Odoo 17?',
    problem: 'AI misses silent API removals — code that "looks fine" but no longer runs.',
    without: {
      badge: 'GUESSING',
      lines: [
        { text: '// AI without version-aware API data', cls: DIM },
        { text: 'The code looks mostly compatible. You might need' },
        { text: 'to update a few imports, but it should work.', cls: BAD },
        { text: '' },
        { text: '  @api.multi              # still fine?', cls: BAD },
        { text: '  def name_get(self):     # still fine?', cls: BAD },
        { text: '' },
        { text: '// no version anchor · silent breakage risk', cls: DIM },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      tokens: '906 tokens',
      lines: [
        { text: '// → find_deprecated_usage(module, target=17.0)', cls: DIM },
        { text: 'DEPRECATION REPORT · 3 blocking', cls: HEAD },
        { text: '✗ @api.multi          removed in 13.0', cls: BAD },
        { text: '   models/sale.py:42 → drop the decorator' },
        { text: '✗ name_get()          → _compute_display_name (17.0)', cls: BAD },
        { text: '   models/sale.py:88 → rename the method' },
        { text: '⚠ <tree>              → <list> since 17.0' },
        { text: '   views/sale_view.xml:15' },
        { text: '' },
        { text: '✓ 3 issues with file:line + fix · 0 false positives', cls: OK },
      ],
    },
  },
  {
    id: 'module-exists',
    persona: 'Consultant',
    tool: 'check_module_exists',
    prompt: 'Does Odoo 17 have built-in loyalty programs? CE or EE?',
    problem: 'AI hedges on edition — the one fact that changes the quote.',
    without: {
      badge: 'UNCERTAIN',
      lines: [
        { text: '// AI hedging without grounding', cls: DIM },
        { text: 'I believe Odoo has a sale_loyalty module,' },
        { text: "but I'm not sure if it's Community or Enterprise.", cls: BAD },
        { text: 'You may want to check the addons store', cls: BAD },
        { text: 'or the latest documentation to be sure.' },
        { text: '' },
        { text: '// no edition answer · blocks the quote', cls: DIM },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      tokens: '147 tokens',
      lines: [
        { text: '// → check_module_exists(loyalty, v=17.0)', cls: DIM },
        { text: '✓ loyalty (CE)       // core loyalty engine', cls: OK },
        { text: '✓ sale_loyalty (CE)  // sale.order integration', cls: OK },
        { text: '✓ pos_loyalty (CE)   // PoS integration', cls: OK },
        { text: '' },
        { text: 'Bundled in Community — no Enterprise license needed' },
        { text: '' },
        { text: '✓ edition confirmed · no EE upsell needed', cls: OK },
      ],
    },
  },
];
