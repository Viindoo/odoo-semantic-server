// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useMemo, useState } from 'react';
import { ReactFlow, Background, Controls, MarkerType, type Node, type Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import IslandErrorBoundary from './IslandErrorBoundary';

type Edition = 'ce' | 'ee' | 'customer';

interface Snapshot {
  generated_at: string;
  model: string;
  version: string;
  definition: { module: string; field_count: number; method_count: number };
  extenders: { module: string; fields_added: number; methods_added: number; edition: Edition }[];
}

const MODELS = ['sale.order', 'account.move', 'res.partner', 'stock.picking'] as const;
type Model = (typeof MODELS)[number];

const editionColor: Record<Edition, { stroke: string; fill: string; label: string }> = {
  ce: { stroke: '#00BBCE', fill: 'rgba(0,187,206,0.10)', label: 'CE' },
  ee: { stroke: '#7F4282', fill: 'rgba(127,66,130,0.10)', label: 'EE' },
  customer: { stroke: '#C99700', fill: 'rgba(201,151,0,0.10)', label: 'CUSTOM' },
};

function nodesFromSnapshot(snap: Snapshot): { nodes: Node[]; edges: Edge[] } {
  const center = { x: 380, y: 230 };
  const definitionNode: Node = {
    id: 'def',
    type: 'default',
    position: center,
    data: {
      label: (
        <div className="text-center">
          <div className="font-mono font-semibold text-sm text-viindoo-on-dark">{snap.model}</div>
          <div className="font-mono text-[10px] text-viindoo-primary mt-1">
            DEFINITION · {snap.definition.module}
          </div>
          <div className="font-mono text-[10px] text-viindoo-on-dark-muted">
            {snap.definition.field_count} fields · {snap.definition.method_count} methods
          </div>
        </div>
      ),
    },
    style: {
      width: 200,
      height: 78,
      background: 'rgba(0,187,206,0.12)',
      border: '2px solid #00BBCE',
      borderRadius: 10,
      padding: 8,
    },
  };

  // Take up to 7 extenders, arrange in circle
  const exts = snap.extenders.slice(0, 7);
  const radius = 220;
  const nodes: Node[] = [definitionNode];
  const edges: Edge[] = [];

  exts.forEach((ext, i) => {
    const angle = (i / exts.length) * 2 * Math.PI - Math.PI / 2;
    const x = center.x + Math.cos(angle) * radius;
    const y = center.y + Math.sin(angle) * radius;
    const color = editionColor[ext.edition];

    nodes.push({
      id: `ext-${i}`,
      type: 'default',
      position: { x, y },
      data: {
        label: (
          <div>
            <div className="font-mono text-xs font-medium text-viindoo-on-dark">{ext.module}</div>
            <div className="font-mono text-[10px] mt-1" style={{ color: color.stroke }}>
              {color.label} · +{ext.fields_added}f · +{ext.methods_added}m
            </div>
          </div>
        ),
      },
      style: {
        width: 170,
        height: 60,
        background: color.fill,
        border: `1.5px solid ${color.stroke}`,
        borderRadius: 8,
        padding: 8,
      },
    });
    edges.push({
      id: `e-${i}`,
      source: 'def',
      target: `ext-${i}`,
      style: { stroke: color.stroke, strokeWidth: 1.5, strokeDasharray: '4 4' },
      animated: true,
      markerEnd: { type: MarkerType.ArrowClosed, color: color.stroke },
    });
  });

  return { nodes, edges };
}

function GraphShowcaseInner() {
  const [model, setModel] = useState<Model>('sale.order');
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    setSnap(null);
    const controller = new AbortController();
    fetch(`/graph-snapshots/${model}.json`, { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setSnap)
      .catch((e: Error) => { if (e.name !== 'AbortError') setError(e.message); });
    return () => controller.abort();
  }, [model]);

  const { nodes, edges } = useMemo(
    () => (snap ? nodesFromSnapshot(snap) : { nodes: [], edges: [] }),
    [snap]
  );
  const generatedDate = snap ? new Date(snap.generated_at).toISOString().slice(0, 10) : '';

  return (
    <div
      data-testid="graph-showcase"
      className="relative w-full aspect-video rounded-2xl border border-white/10 bg-viindoo-bg-2 overflow-hidden"
    >
      {/* Toolbar */}
      <div className="absolute top-4 left-4 right-4 z-10 flex flex-wrap items-center justify-between gap-3">
        <div
          role="tablist"
          aria-label="Model"
          className="inline-flex rounded-lg bg-black/40 border border-white/10 p-1 backdrop-blur"
        >
          {MODELS.map((m, i) => (
            <button
              key={m}
              onClick={() => setModel(m)}
              onKeyDown={(e) => {
                if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                  e.preventDefault();
                  const delta = e.key === 'ArrowRight' ? 1 : -1;
                  const next = (i + delta + MODELS.length) % MODELS.length;
                  setModel(MODELS[next]);
                }
              }}
              tabIndex={model === m ? 0 : -1}
              className={`px-3 py-1.5 text-xs font-mono rounded-md transition ${
                model === m
                  ? 'bg-viindoo-primary text-viindoo-bg-0 font-semibold'
                  : 'text-viindoo-on-dark-muted hover:text-viindoo-on-dark'
              }`}
              data-testid={`graph-tab-${m.replace('.', '-')}`}
              aria-selected={model === m}
              role="tab"
            >
              {m}
            </button>
          ))}
        </div>
        <span className="font-mono text-xs text-viindoo-on-dark-muted bg-black/40 px-2.5 py-1 rounded border border-white/10">
          v {snap?.version || '17.0'} · Drag · Scroll to zoom
        </span>
      </div>

      {/* Graph */}
      <div className="absolute inset-0 pt-16">
        {error && (
          <div className="flex h-full items-center justify-center text-viindoo-on-dark-muted font-mono text-sm">
            Failed to load: {error}
          </div>
        )}
        {snap && (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            nodesDraggable
            zoomOnScroll
            panOnDrag
            proOptions={{ hideAttribution: true }}
          >
            <Background color="rgba(0,187,206,0.10)" gap={32} />
            <Controls position="bottom-left" showInteractive={false} />
          </ReactFlow>
        )}
      </div>

      {/* Generated date */}
      {snap && (
        <div className="absolute bottom-3 right-4 z-10 font-mono text-[10px] text-viindoo-on-dark-dim">
          Generated {generatedDate} from live Neo4j index
        </div>
      )}
    </div>
  );
}

export default function GraphShowcase() {
  return (
    <IslandErrorBoundary name="GraphShowcase">
      <GraphShowcaseInner />
    </IslandErrorBoundary>
  );
}
