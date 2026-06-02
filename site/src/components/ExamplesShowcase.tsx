// SPDX-License-Identifier: AGPL-3.0-or-later
// ExamplesShowcase — the /examples deep-dive island.
// Left rail: pick a real Odoo question. Right: typed prompt + a before/after
// split (ungrounded hallucination vs. graph-verified answer) with token cost.
import { useEffect, useRef, useState } from 'react';
import IslandErrorBoundary from './IslandErrorBoundary';
import { SCENARIOS, type ExampleScenario } from '../lib/examples-data';

function TerminalChrome({ label, badge, tone }: { label: string; badge: string; tone: 'bad' | 'ok' }) {
  return (
    <div className="flex items-center justify-between px-4 py-3 bg-black/30 border-b border-white/10">
      <div className="flex items-center gap-2 text-viindoo-on-dark-muted text-[11px]">
        <div className="flex gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-[#FF5F57]"></span>
          <span className="w-2.5 h-2.5 rounded-full bg-[#FEBC2E]"></span>
          <span className="w-2.5 h-2.5 rounded-full bg-[#27C93F]"></span>
        </div>
        <span className="font-mono">{label}</span>
      </div>
      <span
        className={
          tone === 'ok'
            ? 'text-[10px] px-2 py-0.5 rounded border border-viindoo-success/30 bg-viindoo-success/15 text-viindoo-success font-mono'
            : 'text-[10px] px-2 py-0.5 rounded border border-red-400/30 bg-red-500/15 text-red-300 font-mono'
        }
      >
        {badge}
      </span>
    </div>
  );
}

function CodeBody({ lines }: { lines: { text: string; cls?: string }[] }) {
  return (
    <div className="mt-3 text-viindoo-on-dark space-y-0.5">
      {lines.map((line, i) => (
        <div key={i} className={line.cls || ''}>
          {line.text || ' '}
        </div>
      ))}
    </div>
  );
}

function ExamplesShowcaseInner() {
  const [idx, setIdx] = useState(0);
  const [typed, setTyped] = useState('');
  const current: ExampleScenario = SCENARIOS[idx];
  const panelRef = useRef<HTMLDivElement>(null);

  // Type the prompt out whenever the scenario changes.
  useEffect(() => {
    setTyped('');
    const target = current.prompt;
    let i = 0;
    const timer = setInterval(() => {
      i++;
      setTyped(target.slice(0, i));
      if (i >= target.length) clearInterval(timer);
    }, 22);
    return () => clearInterval(timer);
  }, [idx]);

  return (
    <div data-testid="examples-showcase" className="grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-6">
      {/* Left rail — scenario picker */}
      <div role="tablist" aria-label="Example scenarios" aria-orientation="vertical" className="flex flex-col gap-2">
        {SCENARIOS.map((s, i) => {
          const active = i === idx;
          return (
            <button
              key={s.id}
              role="tab"
              id={`ex-tab-${s.id}`}
              aria-selected={active}
              aria-controls="ex-panel"
              onClick={() => setIdx(i)}
              className={`text-left rounded-xl border p-4 transition ${
                active
                  ? 'border-viindoo-primary/50 bg-viindoo-primary/10 shadow-glow'
                  : 'border-white/10 bg-white/[0.03] hover:bg-white/[0.06] hover:border-white/20'
              }`}
            >
              <div className="flex items-center gap-2 mb-1.5">
                <span className="font-mono text-[10px] uppercase tracking-widest text-viindoo-on-dark-dim">{s.persona}</span>
              </div>
              <div className={`text-sm leading-snug ${active ? 'text-viindoo-on-dark font-medium' : 'text-viindoo-on-dark-muted'}`}>
                {s.prompt}
              </div>
              <div className="mt-2 font-mono text-[11px] text-viindoo-primary-bright">{s.tool}()</div>
            </button>
          );
        })}
      </div>

      {/* Right panel — typed prompt + before/after */}
      <div
        id="ex-panel"
        role="tabpanel"
        aria-labelledby={`ex-tab-${current.id}`}
        ref={panelRef}
        className="min-w-0"
      >
        {/* Prompt bar */}
        <div className="rounded-xl border border-white/10 bg-viindoo-bg-0 px-4 py-3.5 font-mono text-sm">
          <span className="text-viindoo-primary-bright">&gt;_</span>{' '}
          <span className="text-viindoo-on-dark">{typed}</span>
          <span className="inline-block w-1.5 h-3.5 bg-viindoo-primary animate-pulse ml-0.5 align-middle"></span>
        </div>
        <p className="mt-3 text-sm text-viindoo-on-dark-dim">{current.problem}</p>

        <div className="mt-5 grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Without MCP */}
          <div className="rounded-xl border border-white/10 bg-viindoo-bg-0 overflow-hidden font-mono text-xs flex flex-col min-h-[420px]">
            <TerminalChrome label="without-mcp" badge={current.without.badge} tone="bad" />
            <div className="p-4 flex-1 leading-relaxed">
              <CodeBody lines={current.without.lines} />
            </div>
          </div>

          {/* With MCP */}
          <div className="rounded-xl border border-viindoo-primary/20 bg-viindoo-bg-0 overflow-hidden font-mono text-xs flex flex-col min-h-[420px] shadow-glow">
            <TerminalChrome label="with-odoo-semantic" badge={current.with.badge} tone="ok" />
            <div className="p-4 flex-1 leading-relaxed">
              <CodeBody lines={current.with.lines} />
            </div>
            <div className="px-4 py-2.5 border-t border-white/10 flex items-center justify-between text-[11px] text-viindoo-on-dark-dim">
              <span>structured · tree-grammar output</span>
              <span className="font-semibold text-viindoo-primary-bright">{current.with.tokens}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ExamplesShowcase() {
  return (
    <IslandErrorBoundary name="ExamplesShowcase">
      <ExamplesShowcaseInner />
    </IslandErrorBoundary>
  );
}
