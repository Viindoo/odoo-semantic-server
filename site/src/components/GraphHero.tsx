import { useEffect, useRef, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  MarkerType,
} from '@xyflow/react';
// framer-motion useAnimate available for future frame transitions

import '@xyflow/react/dist/style.css';

interface GraphSnapshot {
  nodes: Node[];
  edges: Edge[];
}

const PLACEHOLDER_SNAPSHOT: GraphSnapshot = {
  nodes: [
    {
      id: 'sale.order:sale',
      type: 'default',
      position: { x: 300, y: 150 },
      data: { label: 'sale.order\n(sale)', module: 'sale', is_definition: true, field_count: 148 },
      style: {
        background: '#7c3aed',
        color: '#fff',
        border: '2px solid #5b21b6',
        borderRadius: 8,
        padding: '8px 12px',
        minWidth: 160,
        fontFamily: 'monospace',
        fontSize: 13,
      },
    },
    {
      id: 'sale.order:viin_sale',
      type: 'default',
      position: { x: 100, y: 300 },
      data: { label: 'sale.order\n(viin_sale)', module: 'viin_sale' },
      style: {
        background: '#1d4ed8',
        color: '#fff',
        border: '2px solid #1e40af',
        borderRadius: 8,
        padding: '8px 12px',
        minWidth: 160,
        fontFamily: 'monospace',
        fontSize: 13,
      },
    },
    {
      id: 'sale.order:sale_management',
      type: 'default',
      position: { x: 500, y: 300 },
      data: { label: 'sale.order\n(sale_management)', module: 'sale_management' },
      style: {
        background: '#0e7490',
        color: '#fff',
        border: '2px solid #0c4a6e',
        borderRadius: 8,
        padding: '8px 12px',
        minWidth: 180,
        fontFamily: 'monospace',
        fontSize: 13,
      },
    },
    {
      id: 'sale.order:website_sale',
      type: 'default',
      position: { x: 300, y: 420 },
      data: { label: 'sale.order\n(website_sale)', module: 'website_sale' },
      style: {
        background: '#166534',
        color: '#fff',
        border: '2px solid #14532d',
        borderRadius: 8,
        padding: '8px 12px',
        minWidth: 180,
        fontFamily: 'monospace',
        fontSize: 13,
      },
    },
  ],
  edges: [
    {
      id: 'e-viin_sale-sale',
      source: 'sale.order:viin_sale',
      target: 'sale.order:sale',
      type: 'smoothstep',
      label: 'INHERITS',
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke: '#7c3aed', strokeWidth: 2 },
      labelStyle: { fill: '#7c3aed', fontSize: 11 },
    },
    {
      id: 'e-sale_management-sale',
      source: 'sale.order:sale_management',
      target: 'sale.order:sale',
      type: 'smoothstep',
      label: 'INHERITS',
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke: '#0e7490', strokeWidth: 2 },
      labelStyle: { fill: '#0e7490', fontSize: 11 },
    },
    {
      id: 'e-website_sale-sale',
      source: 'sale.order:website_sale',
      target: 'sale.order:sale',
      type: 'smoothstep',
      label: 'INHERITS',
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke: '#166534', strokeWidth: 2 },
      labelStyle: { fill: '#166534', fontSize: 11 },
    },
  ],
};

function nodeIdsForFrame(frame: number): Set<string> {
  const frames = [
    new Set(['sale.order:sale']),
    new Set(['sale.order:sale', 'sale.order:viin_sale']),
    new Set(['sale.order:sale', 'sale.order:viin_sale', 'sale.order:sale_management']),
    new Set([
      'sale.order:sale',
      'sale.order:viin_sale',
      'sale.order:sale_management',
      'sale.order:website_sale',
    ]),
    new Set([
      'sale.order:sale',
      'sale.order:viin_sale',
      'sale.order:sale_management',
      'sale.order:website_sale',
    ]),
  ];
  return frames[Math.min(frame, frames.length - 1)];
}

function edgeIdsForFrame(frame: number): Set<string> {
  const frames: Set<string>[] = [
    new Set<string>(),
    new Set(['e-viin_sale-sale']),
    new Set(['e-viin_sale-sale', 'e-sale_management-sale']),
    new Set(['e-viin_sale-sale', 'e-sale_management-sale', 'e-website_sale-sale']),
    new Set(['e-viin_sale-sale', 'e-sale_management-sale', 'e-website_sale-sale']),
  ];
  return frames[Math.min(frame, frames.length - 1)];
}

export default function GraphHero() {
  const [snapshot, setSnapshot] = useState<GraphSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [fieldBadge, setFieldBadge] = useState(false);
  const [pulsing, setPulsing] = useState(false);
  const [interactive, setInteractive] = useState(false);
  const frameRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch('/graph-snapshot.json')
      .then((r) => r.json())
      .then((data: GraphSnapshot) => {
        setSnapshot(data);
        setLoading(false);
      })
      .catch(() => {
        setSnapshot(PLACEHOLDER_SNAPSHOT);
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    if (loading || !snapshot) return;

    // 5-frame cinematic auto-reveal: each frame = 1s
    const advanceFrame = () => {
      frameRef.current += 1;
      const f = frameRef.current;
      setCurrentFrame(f);

      if (f === 3) setFieldBadge(true);
      if (f === 4) {
        setPulsing(true);
        setTimeout(() => setPulsing(false), 900);
      }
      if (f >= 5) {
        if (timerRef.current) clearInterval(timerRef.current);
        setInteractive(true);
      }
    };

    timerRef.current = setInterval(advanceFrame, 1000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [loading, snapshot]);

  if (loading || !snapshot) {
    return (
      <div className="flex items-center justify-center h-64 bg-gray-950 rounded-xl border border-gray-800">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-gray-400 text-sm font-mono">Loading graph...</span>
        </div>
      </div>
    );
  }

  const visibleNodeIds = nodeIdsForFrame(currentFrame);
  const visibleEdgeIds = edgeIdsForFrame(currentFrame);

  const shownNodes = snapshot.nodes.filter((n) => visibleNodeIds.has(n.id));
  const shownEdges = snapshot.edges.filter(
    (e) =>
      visibleEdgeIds.has(e.id) &&
      (pulsing && e.id === snapshot.edges[snapshot.edges.length - 1]?.id
        ? { ...e, animated: true }
        : true)
  );

  // Pulse last edge on frame 4
  const displayEdges = shownEdges.map((e) => {
    if (pulsing && e.id === 'e-website_sale-sale') {
      return { ...e, animated: true, style: { ...(e.style || {}), stroke: '#f59e0b', strokeWidth: 3 } };
    }
    return e;
  });

  const definitionNode = snapshot.nodes.find((n) => n.data?.is_definition);
  const fieldCount = (definitionNode?.data?.field_count as number | undefined) ?? 148;

  return (
    <div className="relative w-full h-96 bg-gray-950 rounded-xl border border-gray-800 overflow-hidden group">
      {/* Frame label */}
      {!interactive && (
        <div className="absolute top-3 left-3 z-10 text-xs font-mono text-gray-500 bg-gray-900/80 px-2 py-1 rounded">
          Frame {Math.min(currentFrame + 1, 5)}/5
        </div>
      )}

      {/* Field count badge — appears on frame 4 */}
      {fieldBadge && (
        <div
          className="absolute top-3 right-3 z-10 bg-violet-600 text-white text-xs font-bold px-3 py-1 rounded-full shadow-lg"
          style={{
            animation: 'fadeInScale 0.4s ease-out forwards',
          }}
        >
          {fieldCount} fields
        </div>
      )}

      {/* Model name badge */}
      <div className="absolute bottom-3 left-3 z-10 text-xs font-mono text-gray-400 bg-gray-900/80 px-2 py-1 rounded">
        sale.order — inheritance chain
      </div>

      {interactive && (
        <div className="absolute bottom-3 right-3 z-10 text-xs text-gray-500 bg-gray-900/80 px-2 py-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
          drag / scroll to explore
        </div>
      )}

      <ReactFlow
        nodes={shownNodes}
        edges={displayEdges}
        fitView
        nodesDraggable={interactive}
        nodesConnectable={false}
        elementsSelectable={interactive}
        panOnDrag={interactive}
        zoomOnScroll={interactive}
        zoomOnPinch={interactive}
        zoomOnDoubleClick={false}
        preventScrolling={!interactive}
        colorMode="dark"
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1f2937" gap={20} />
        {interactive && <Controls showInteractive={false} />}
      </ReactFlow>

      <style>{`
        @keyframes fadeInScale {
          from { opacity: 0; transform: scale(0.7); }
          to   { opacity: 1; transform: scale(1); }
        }
      `}</style>
    </div>
  );
}
