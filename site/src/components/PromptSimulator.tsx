// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react';
import IslandErrorBoundary from './IslandErrorBoundary';

interface Example {
  prompt: string;
  without: { badge: string; lines: { text: string; cls?: string }[] };
  with: { badge: string; lines: { text: string; cls?: string }[] };
}

const EXAMPLES: Example[] = [
  {
    prompt: 'List all modules extending sale.order in Odoo 17',
    without: {
      badge: 'HALLUCINATED',
      lines: [
        { text: '// AI guessing from training data', cls: 'text-viindoo-on-dark-dim' },
        { text: 'In Odoo 17, sale.order is typically extended by:' },
        { text: "  • sale_advance_payment  ⚠ module doesn't exist", cls: 'text-red-400 line-through' },
        { text: '  • sale_management' },
        { text: '  • sale_workflow  ⚠ removed in v15', cls: 'text-red-400 line-through' },
        { text: '  • sale_stock' },
        { text: '  • sale_crm  ⚠ no such module', cls: 'text-red-400 line-through' },
        { text: '  • sale_loyalty' },
        { text: '' },
        { text: '// 3 of 6 modules are fabricated', cls: 'text-viindoo-on-dark-dim' },
        { text: '// version mixing v13/v15/v17', cls: 'text-viindoo-on-dark-dim' },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      lines: [
        { text: '// → resolve_model(sale.order, v=17.0)', cls: 'text-viindoo-on-dark-dim' },
        { text: 'sale.order [DEFINITION]', cls: 'text-viindoo-primary-bright font-semibold' },
        { text: '├─ defined_in: sale' },
        { text: '├─ total_fields: 148' },
        { text: '├─ total_methods: 62' },
        { text: '└─ extended_by:' },
        { text: '   ├─ sale_management   // +6 fields, +3 methods' },
        { text: '   ├─ sale_stock        // +12 fields, +7 methods' },
        { text: '   ├─ sale_loyalty      // +4 fields, +2 methods' },
        { text: '   ├─ sale_subscription // EE-only, +18 fields' },
        { text: '   ├─ sale_purchase     // +3 fields, +5 methods' },
        { text: '   └─ tvtmaaddons17     // customer addon' },
        { text: '' },
        { text: '✓ 6 modules · all verified against Neo4j index', cls: 'text-viindoo-success' },
      ],
    },
  },
  {
    prompt: 'Does Odoo 17 have built-in loyalty programs?',
    without: {
      badge: 'UNCERTAIN',
      lines: [
        { text: '// AI hedging without grounding', cls: 'text-viindoo-on-dark-dim' },
        { text: 'I believe Odoo has a sale_loyalty module,' },
        { text: "but I'm not sure if it's in Community or Enterprise.", cls: 'text-red-400' },
        { text: 'You may need to check the addons store', cls: 'text-red-400' },
        { text: 'or consult the latest documentation.' },
        { text: '' },
        { text: '// no version anchor · no module path', cls: 'text-viindoo-on-dark-dim' },
        { text: '// requires manual verification', cls: 'text-viindoo-on-dark-dim' },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      lines: [
        { text: '// → check_module_exists(loyalty, v=17.0)', cls: 'text-viindoo-on-dark-dim' },
        { text: '✓ loyalty (CE)         // core loyalty engine', cls: 'text-viindoo-success' },
        { text: '✓ sale_loyalty (CE)    // sale.order integration', cls: 'text-viindoo-success' },
        { text: '✓ pos_loyalty (CE)     // PoS integration', cls: 'text-viindoo-success' },
        { text: '' },
        { text: 'Available since v15.0 · Free in Community edition' },
        { text: '' },
        { text: '✓ verified against module index', cls: 'text-viindoo-success' },
      ],
    },
  },
  {
    prompt: 'Impact of changing sale.order.amount_total semantics',
    without: {
      badge: 'INCOMPLETE',
      lines: [
        { text: '// AI without dependency graph', cls: 'text-viindoo-on-dark-dim' },
        { text: 'amount_total is a computed field that aggregates' },
        { text: 'order_line.price_subtotal. Changing it would affect:' },
        { text: '  - reporting queries (which ones? unclear)', cls: 'text-red-400' },
        { text: '  - invoicing logic (where exactly?)', cls: 'text-red-400' },
        { text: '  - maybe accounting integration', cls: 'text-red-400' },
        { text: '' },
        { text: '// vague · no concrete file refs', cls: 'text-viindoo-on-dark-dim' },
        { text: '// agent must grep manually', cls: 'text-viindoo-on-dark-dim' },
      ],
    },
    with: {
      badge: 'VERIFIED · GRAPH',
      lines: [
        { text: '// → impact_analysis(sale.order, amount_total)', cls: 'text-viindoo-on-dark-dim' },
        { text: 'BLAST RADIUS · sale.order.amount_total', cls: 'text-viindoo-primary-bright font-semibold' },
        { text: '├─ modules affected: 12' },
        { text: '├─ method overrides: 38' },
        { text: '├─ view references: 49' },
        { text: '├─ report references: 14' },
        { text: '└─ key dependents:' },
        { text: '   ├─ account.move (3 hooks)' },
        { text: '   ├─ sale_loyalty (1 hook)' },
        { text: '   └─ sale_subscription (2 hooks)' },
        { text: '' },
        { text: '✓ full chain · 3,218 tokens', cls: 'text-viindoo-success' },
      ],
    },
  },
];

function PromptSimulatorInner() {
  const [idx, setIdx] = useState(0);
  const [typed, setTyped] = useState('');
  const current = EXAMPLES[idx];

  useEffect(() => {
    setTyped('');
    const target = current.prompt;
    let i = 0;
    const timer = setInterval(() => {
      i++;
      setTyped(target.slice(0, i));
      if (i >= target.length) clearInterval(timer);
    }, 25);
    return () => clearInterval(timer);
  }, [idx, current.prompt]);

  // Auto-rotate every 12s
  useEffect(() => {
    const timer = setTimeout(() => setIdx((idx + 1) % EXAMPLES.length), 12000);
    return () => clearTimeout(timer);
  }, [idx]);

  return (
    <div data-testid="prompt-simulator" className="space-y-4">
      {/* Prompt selector */}
      <div role="group" aria-label="Prompt examples" className="flex flex-wrap gap-2 mb-4">
        {EXAMPLES.map((_ex, i) => (
          <button
            key={i}
            onClick={() => setIdx(i)}
            aria-pressed={idx === i}
            aria-label={`Show example ${i + 1}`}
            className={`px-3 py-1.5 text-xs font-mono rounded-md transition ${idx === i ? 'bg-viindoo-primary text-viindoo-bg-0 font-semibold' : 'bg-white/5 border border-white/10 text-viindoo-on-dark-muted hover:text-viindoo-on-dark'}`}
          >
            Example {i + 1}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Without MCP */}
        <div className="rounded-xl border border-white/10 bg-viindoo-bg-0 overflow-hidden font-mono text-xs flex flex-col min-h-[460px]">
          <div className="flex items-center justify-between px-4 py-3 bg-black/30 border-b border-white/10">
            <div className="flex items-center gap-2 text-viindoo-on-dark-muted text-[11px]">
              <div className="flex gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-[#FF5F57]"></span>
                <span className="w-2.5 h-2.5 rounded-full bg-[#FEBC2E]"></span>
                <span className="w-2.5 h-2.5 rounded-full bg-[#27C93F]"></span>
              </div>
              <span>without-mcp</span>
            </div>
            <span className="text-[10px] px-2 py-0.5 rounded border border-red-400/30 bg-red-500/15 text-red-300">
              {current.without.badge}
            </span>
          </div>
          <div className="p-4 flex-1 leading-relaxed">
            <div className="text-viindoo-primary-bright">
              &gt;_{' '}
              <span className="text-viindoo-on-dark">
                {typed}
                <span className="inline-block w-1.5 h-3 bg-viindoo-primary animate-pulse ml-0.5"></span>
              </span>
            </div>
            <div className="mt-3 text-viindoo-on-dark space-y-0.5">
              {current.without.lines.map((line, i) => (
                <div key={i} className={line.cls || ''}>
                  {line.text || ' '}
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* With MCP */}
        <div className="rounded-xl border border-white/10 bg-viindoo-bg-0 overflow-hidden font-mono text-xs flex flex-col min-h-[460px]">
          <div className="flex items-center justify-between px-4 py-3 bg-black/30 border-b border-white/10">
            <div className="flex items-center gap-2 text-viindoo-on-dark-muted text-[11px]">
              <div className="flex gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-[#FF5F57]"></span>
                <span className="w-2.5 h-2.5 rounded-full bg-[#FEBC2E]"></span>
                <span className="w-2.5 h-2.5 rounded-full bg-[#27C93F]"></span>
              </div>
              <span>with-odoo-semantic</span>
            </div>
            <span className="text-[10px] px-2 py-0.5 rounded border border-viindoo-success/30 bg-viindoo-success/15 text-viindoo-success">
              {current.with.badge}
            </span>
          </div>
          <div className="p-4 flex-1 leading-relaxed">
            <div className="text-viindoo-primary-bright">
              &gt;_ <span className="text-viindoo-on-dark">{typed}</span>
            </div>
            <div className="mt-3 text-viindoo-on-dark space-y-0.5">
              {current.with.lines.map((line, i) => (
                <div key={i} className={line.cls || ''}>
                  {line.text || ' '}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function PromptSimulator() {
  return (
    <IslandErrorBoundary name="PromptSimulator">
      <PromptSimulatorInner />
    </IslandErrorBoundary>
  );
}
