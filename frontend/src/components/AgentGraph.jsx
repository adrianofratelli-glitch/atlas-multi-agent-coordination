import { useMemo } from 'react';
import { layoutGraph, edgePath, rectReach, describeTurn, fitText, nodeSubtitle, HUB } from '../graphModel.js';
import { useReducedMotion } from '../hooks.js';

const VIEW = { width: 700, height: 370 };
const NODE = { w: 128, h: 44 };
const HUB_NODE = { w: 154, h: 56 };
const EDGE_KINDS = ['route', 'handoff', 'revisit', 'fanout', 'return', 'degraded'];
const KIND_LABELS = {
  route: 'roteamento', handoff: 'handoff', revisit: 'retorno controlado', fanout: 'despacho paralelo',
  return: 'entrega ao orquestrador', degraded: 'handoff degradado',
};

function size(node) {
  return node.hub ? HUB_NODE : NODE;
}

// Text padding inside a node (viewBox units): keeps glyphs clear of the pill's rounded ends.
const PAD = { label: 24, sub: 28 };
const FONT = { label: 13.5, hubLabel: 15, sub: 10 };

function LegendLine({ kind, dashed = false }) {
  return (
    <svg width="22" height="8" aria-hidden="true">
      <line x1="0" y1="4" x2="22" y2="4" className={`legend-line edge--${kind}`} strokeDasharray={dashed ? '3 4' : undefined} />
    </svg>
  );
}

function Legend() {
  return (
    <ul className="graph-legend" aria-label="Legenda do grafo">
      <li><LegendLine kind="route" />orquestrador escolhe quem começa</li>
      <li><LegendLine kind="handoff" />um agente passa a vez a outro</li>
      <li><LegendLine kind="revisit" />volta a um agente para confirmar</li>
      <li><LegendLine kind="fanout" />agentes em paralelo</li>
      <li><LegendLine kind="return" dashed />parte entregue ao orquestrador</li>
      <li><span className="legend-badge legend-badge--plan">1</span>ordem no plano</li>
      <li><span className="legend-badge legend-badge--write" />gravou dado do cliente</li>
      <li><span className="legend-badge legend-badge--paused" />aguarda analista</li>
      <li><span className="legend-chip">2 tools</span>ferramentas usadas (passe o mouse no agente)</li>
    </ul>
  );
}

/**
 * O grafo ao vivo do turno: orquestrador no centro, especialistas do registry em volta.
 * Tudo o que ele desenha sai de `deriveTurn` (graphModel.js) — nada é inventado na tela.
 */
export default function AgentGraph({ agents = [], turn, live = false, labels = {}, highlight = null }) {
  const reducedMotion = useReducedMotion();
  const graph = useMemo(() => layoutGraph(agents, { ...VIEW, padX: 80, padY: 46 }), [agents]);
  const visitCounts = useMemo(() => (turn?.path || []).reduce((acc, key) => ({ ...acc, [key]: (acc[key] || 0) + 1 }), {}), [turn]);
  const edges = (turn?.edges || []).filter((edge) => graph.byKey[edge.from] && graph.byKey[edge.to]);
  const maxStep = edges.reduce((max, edge) => Math.max(max, edge.step), 0);
  const caption = describeTurn(turn, { live, labels });
  const pathLabels = (turn?.path || []).map((key) => labels[key] || key);
  const centre = graph.byKey[HUB];
  const focusNodes = new Set(highlight?.nodes || []);
  const focusEdges = highlight?.edges || [];
  const hasFocus = focusNodes.size > 0 || focusEdges.length > 0;
  const edgeInFocus = (edge) => focusEdges.some((item) => item.from === edge.from && item.to === edge.to);

  return (
    <figure className={`agent-graph ${live ? 'is-live' : ''}`} aria-busy={live || undefined}>
      <svg viewBox={`0 0 ${VIEW.width} ${VIEW.height}`} className={`agent-graph-svg ${hasFocus ? 'has-focus' : ''}`} role="img" aria-labelledby="agent-graph-title agent-graph-desc">
        <title id="agent-graph-title">Grafo de coordenação do turno</title>
        <desc id="agent-graph-desc">{caption || 'Nenhum turno em andamento.'} {pathLabels.length ? `Caminho: ${pathLabels.join(', ')}.` : ''}</desc>
        <defs>
          {EDGE_KINDS.map((kind) => (
            <marker key={kind} id={`arrow-${kind}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 1 L9 5 L0 9 z" className={`arrow-head edge--${kind}`} />
            </marker>
          ))}
        </defs>

        <ellipse className="graph-orbit" cx={centre.x} cy={centre.y} rx={VIEW.width / 2 - 80} ry={VIEW.height / 2 - 46} />
        {graph.nodes.filter((node) => !node.hub).map((node) => (
          <line key={`spoke-${node.key}`} className="graph-spoke" x1={centre.x} y1={centre.y} x2={node.x} y2={node.y} />
        ))}

        {edges.map((edge) => {
          const from = graph.byKey[edge.from];
          const to = graph.byKey[edge.to];
          const fs = size(from); const ts = size(to);
          const bend = edge.kind === 'route' || edge.kind === 'fanout' ? 0.06 : edge.kind === 'return' ? -0.1 : 0.18;
          const { d } = edgePath(from, to, { bend, radiusFrom: rectReach(fs.w / 2, fs.h / 2), radiusTo: rectReach(ts.w / 2, ts.h / 2) });
          const fresh = live && edge.step === maxStep;
          const key = `${edge.from}-${edge.to}-${edge.step}`;
          return (
            <g key={key} className={`graph-edge edge--${edge.kind} ${fresh ? 'is-fresh' : ''} ${edgeInFocus(edge) ? 'is-focus' : ''}`}
              style={live ? undefined : { '--edge-delay': `${Math.min(edge.step, 8) * 110}ms` }}>
              <title>{`${KIND_LABELS[edge.kind]}: ${labels[edge.from] || edge.from} → ${labels[edge.to] || edge.to}${edge.task ? ` — ${edge.task}` : ''}`}</title>
              <path d={d} pathLength="1" className="edge-line" markerEnd={`url(#arrow-${edge.kind})`} />
              {fresh && !reducedMotion && (
                <circle r="4.5" className="edge-pulse">
                  <animateMotion dur="0.9s" repeatCount="1" fill="freeze" path={d} />
                </circle>
              )}
            </g>
          );
        })}

        {graph.nodes.map((node) => {
          const { w, h } = size(node);
          const visited = node.hub ? (turn?.edges || []).length > 0 : (turn?.visited || []).includes(node.key);
          const isActive = live && turn?.active === node.key;
          const paused = turn?.paused?.agent === node.key;
          const writes = turn?.writes?.[node.key] || [];
          const toolsUsed = turn?.tools?.[node.key] || [];
          const planStep = turn?.planIndex?.[node.key];
          const visits = visitCounts[node.key] || 0;
          const sub = nodeSubtitle({ active: node.active, paused, hub: node.hub, isActive, visited, tools: toolsUsed,
            consolidated: turn?.consolidated, planLength: turn?.plan?.length || 0 });
          const labelFont = node.hub ? FONT.hubLabel : FONT.label;
          const suffix = visits > 1 ? ` ×${visits}` : '';
          const label = `${fitText(node.label, w - PAD.label - suffix.length * labelFont * 0.6, labelFont)}${suffix}`;
          const subText = fitText(sub, w - PAD.sub, FONT.sub);
          const tooltip = [
            `${node.label} (${node.key})${node.model ? ` · ${node.model}` : ''}`,
            node.active ? '' : 'desligado no registry',
            toolsUsed.length ? `tools neste turno: ${toolsUsed.join(', ')}` : '',
            writes.length ? `gravou em: ${writes.join(', ')}` : '',
            planStep ? `passo ${planStep} do plano` : '',
          ].filter(Boolean).join('\n');
          const classes = ['graph-node', node.hub ? 'is-hub' : '', node.active ? '' : 'is-inactive', visited ? 'is-visited' : '',
            isActive ? 'is-active' : '', paused ? 'is-paused' : '', writes.length ? 'has-write' : '',
            focusNodes.has(node.key) ? 'is-focus' : ''].filter(Boolean).join(' ');
          return (
            <g key={node.key} className={classes} transform={`translate(${node.x} ${node.y})`}>
              <title>{tooltip}</title>
              {isActive && <rect className="node-pulse" x={-w / 2 - 5} y={-h / 2 - 5} width={w + 10} height={h + 10} rx={(h + 10) / 2} />}
              <rect className="node-body" x={-w / 2} y={-h / 2} width={w} height={h} rx={node.hub ? 14 : h / 2} />
              <text className="node-label" y={subText ? -2 : 4} textAnchor="middle">{label}</text>
              {subText && <text className="node-sub" y={13} textAnchor="middle">{subText}</text>}
              {planStep && (
                <g className="node-badge node-badge--plan" transform={`translate(${-w / 2 + 6} ${-h / 2 + 1})`}>
                  <circle r="9" />
                  <text y="3.5" textAnchor="middle">{planStep}</text>
                </g>
              )}
              {writes.length > 0 && (
                <g className="node-badge node-badge--write" transform={`translate(${w / 2 - 6} ${-h / 2 + 1})`}>
                  <circle r="9" />
                  <path d="M-4 4l.5-2.3 5-5a1 1 0 0 1 1.4 0l.4.4a1 1 0 0 1 0 1.4l-5 5z" />
                </g>
              )}
              {paused && (
                <g className="node-paused-tag" transform={`translate(0 ${h / 2 + 13})`}>
                  <rect x="-70" y="-10" width="140" height="20" rx="10" />
                  <g transform="translate(-60 -6.5)">
                    <rect x="1.5" y="5" width="9" height="7" rx="1.4" />
                    <path d="M3.5 5V3.5a2.5 2.5 0 0 1 5 0V5" fill="none" />
                  </g>
                  <text x="7" y="4" textAnchor="middle">aguardando analista</text>
                </g>
              )}
            </g>
          );
        })}
      </svg>
      <figcaption className="agent-graph-caption">
        <p className="graph-status" aria-live="polite">{caption || 'Envie uma mensagem: o caminho do turno aparece neste grafo.'}</p>
        {pathLabels.length > 0 && (
          <ol className={`graph-path ${turn?.fanout ? 'is-parallel' : ''}`} aria-label={turn?.fanout ? 'Agentes despachados em paralelo' : 'Ordem de atuação dos agentes'}>
            {pathLabels.map((name, index) => <li key={`${name}-${index}`}>{name}</li>)}
          </ol>
        )}
        <Legend />
      </figcaption>
    </figure>
  );
}
