import { useEffect, useRef, useState } from 'react';
import IslandErrorBoundary from './IslandErrorBoundary';

interface BenchmarkCase {
  id: string;
  persona: string;
  query: string;
  without_mcp: { tokens: number; steps?: string[]; note?: string };
  with_mcp: { tokens: number; tool?: string; note?: string };
  savings_pct: number;
}

interface BenchmarkData {
  measured_at: string;
  tokenizer: string;
  methodology?: string;
  cases: BenchmarkCase[];
}

const CASE_TITLES: Record<string, string> = {
  'find-override-point': 'Find override point',
  'check-module-exists': 'Check standard feature',
  'impact-analysis-field': 'Impact analysis',
};

function formatTokens(n: number): string {
  return n.toLocaleString('en-US');
}

function BenchmarkCard({ c, index, animate }: { c: BenchmarkCase; index: number; animate: boolean }) {
  const ratio = c.with_mcp.tokens / c.without_mcp.tokens;
  const withWidth = Math.max(2, ratio * 100); // min 2% visual clamp
  const savings = c.savings_pct.toFixed(1);

  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-7 shadow-sm">
      <div className="mb-4 inline-flex h-8 w-8 items-center justify-center rounded-full bg-viindoo-primary/10 border border-viindoo-primary text-viindoo-primary font-mono font-semibold text-xs">
        0{index + 1}
      </div>
      <h3 className="font-display text-lg font-bold text-viindoo-dark leading-tight">{CASE_TITLES[c.id] || c.id}</h3>
      <p className="font-mono text-[10px] uppercase tracking-widest text-viindoo-primary-deep mt-1 mb-4">{c.persona}</p>
      <div className="rounded bg-gray-50 border-l-2 border-viindoo-primary p-3 font-mono text-xs text-viindoo-body mb-5">
        &ldquo;{c.query}&rdquo;
      </div>

      <div className="space-y-3.5">
        <div>
          <div className="flex justify-between font-mono text-[11px] mb-1">
            <span className="text-viindoo-muted">Without MCP</span>
            <span className="text-viindoo-dark font-semibold">~{formatTokens(c.without_mcp.tokens)} tk</span>
          </div>
          <div className="h-2.5 rounded-full bg-gray-100 overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-1000 ease-out"
              style={{ width: animate ? '100%' : '0%', background: 'linear-gradient(90deg, #B43A3A, #FF5F57)' }}
            ></div>
          </div>
        </div>
        <div>
          <div className="flex justify-between font-mono text-[11px] mb-1">
            <span className="text-viindoo-muted">With MCP</span>
            <span className="text-viindoo-dark font-semibold">~{formatTokens(c.with_mcp.tokens)} tk</span>
          </div>
          <div className="h-2.5 rounded-full bg-gray-100 overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-1000 ease-out"
              style={{ width: animate ? `${withWidth}%` : '0%', background: 'linear-gradient(90deg, #00BBCE, #2DD4E8)' }}
            ></div>
          </div>
        </div>
      </div>

      <div className="mt-5 flex items-center justify-between rounded-lg border border-viindoo-success/30 bg-viindoo-success/10 px-3 py-2.5">
        <span className="text-xs uppercase tracking-wider text-viindoo-muted font-mono">Saved</span>
        <span className="font-mono text-xl font-bold text-viindoo-success">−{savings}%</span>
      </div>
    </div>
  );
}

function TokenBenchmarkInner() {
  const [data, setData] = useState<BenchmarkData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [animate, setAnimate] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch('/benchmark-data.json', { signal: controller.signal })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(setData)
      .catch((e: Error) => { if (e.name !== 'AbortError') setError(e.message); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!data || !rootRef.current) return;
    const obs = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) setAnimate(true);
    }, { threshold: 0.2 });
    obs.observe(rootRef.current);
    return () => obs.disconnect();
  }, [data]);

  // The data-testid="token-benchmark" wrapper renders unconditionally so that
  // both SSR output and pre-fetch hydration phase always expose the slot in
  // the DOM. Browser tests use this testid as a scroll target — if it only
  // appeared after the JSON fetch resolved, `scroll_into_view_if_needed`
  // would race against `client:visible` hydration + fetch and time out in CI.
  // This matches the pattern used by GraphShowcase and PromptSimulator.
  return (
    <div ref={rootRef} data-testid="token-benchmark">
      {error ? (
        <div role="alert" className="text-viindoo-muted font-mono text-sm">
          Benchmark unavailable: {error}
        </div>
      ) : !data ? (
        <div className="text-viindoo-muted font-mono text-sm">Loading benchmark&hellip;</div>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
            {data.cases.map((c, i) => (
              <BenchmarkCard key={c.id} c={c} index={i} animate={animate} />
            ))}
          </div>
          <p className="mt-6 text-center font-mono text-xs text-viindoo-muted">
            Measured with tiktoken <code>{data.tokenizer}</code> on {data.measured_at} &middot;{' '}
            <a href="/benchmarks/" className="text-viindoo-secondary hover:underline">methodology &rarr;</a>
          </p>
        </>
      )}
    </div>
  );
}

export default function TokenBenchmark() {
  return (
    <IslandErrorBoundary name="TokenBenchmark">
      <TokenBenchmarkInner />
    </IslandErrorBoundary>
  );
}
