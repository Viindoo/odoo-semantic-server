// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react';
import IslandErrorBoundary from './IslandErrorBoundary';
import { SCENARIOS } from '../lib/examples-data';

// Landing teaser: the first 3 scenarios sourced from the shared SSOT
// (examples-data.ts). The full set lives on /examples — this component must
// never keep a private copy (single source of truth: tool names, counts, copy
// all stay in sync with the /examples showcase).
const EXAMPLES = SCENARIOS.slice(0, 3);

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
  }, [idx]);

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
                  {line.text || ' '}
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
                  {line.text || ' '}
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
