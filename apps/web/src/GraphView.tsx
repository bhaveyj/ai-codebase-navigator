import { memo, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Background, BackgroundVariant, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider, useReactFlow, type Node, type NodeProps } from '@xyflow/react';
import { Box, Braces, FileCode2, Folder, GitFork, Loader2, Package, RotateCcw, SlidersHorizontal } from 'lucide-react';
import '@xyflow/react/dist/style.css';
import ELK from 'elkjs/lib/elk-api';
import elkWorkerUrl from 'elkjs/lib/elk-worker.min.js?url';
import { api, enc } from './api';
import type { CodeNode, Direction, GraphData, GraphLevel } from './types';

type FlowNode = Node<{code: CodeNode; highlighted: boolean; vertical: boolean}, 'code'>;
export function NodeIcon({node, size = 16}: {node: Pick<CodeNode, 'kind'|'symbolKind'>; size?: number}) {
  const Icon = node.kind === 'module' ? Folder : node.kind === 'repository' ? GitFork : node.kind === 'external' ? Package : node.kind === 'file' ? FileCode2 : node.symbolKind === 'class' ? Box : Braces;
  return <Icon size={size}/>;
}
const CodeGraphNode = memo(({data, selected}: NodeProps<FlowNode>) => <div className={`code-node kind-${data.code.kind} ${selected ? 'selected' : ''} ${data.highlighted ? 'evidence-node' : ''}`}>
  <Handle type="target" position={data.vertical ? Position.Top : Position.Left}/><div className="node-top"><span className="node-icon"><NodeIcon node={data.code}/></span><span className="node-kind">{data.code.symbolKind || data.code.kind}</span>{data.code.exported && <span className="export-dot" title="Exported symbol"/>}</div>
  <div className="node-name" title={data.code.path || data.code.name}>{data.code.name}</div><Handle type="source" position={data.vertical ? Position.Bottom : Position.Right}/>
</div>);
const nodeTypes = {code: CodeGraphNode};
const edgeColors: Record<string, string> = {contains: '#35434a', imports: '#53818a', reexports: '#8884bb', references: '#6e83c9', calls: '#c79a63'};

interface Props {analysisId: string; level: GraphLevel; setLevel: (level: GraphLevel) => void; focus: string; setFocus: (id: string) => void; direction: Direction; setDirection: (value: Direction) => void; selected: CodeNode | null; onSelect: (node: CodeNode) => void; onDrill: (node: CodeNode) => void; highlights: string[]; languages: string[]; }
function GraphInner(props: Props) {
  const {analysisId, level, setLevel, focus, setFocus, direction, setDirection, selected, onSelect, onDrill, highlights, languages} = props;
  const [language, setLanguage] = useState(''); const [kind, setKind] = useState(''); const [filters, setFilters] = useState(false);
  const [positions, setPositions] = useState<Record<string, {x: number; y: number}>>({}); const [layoutPending, setLayoutPending] = useState(false);
  const {fitView} = useReactFlow();
  const graph = useQuery({queryKey: ['graph', analysisId, level, focus, direction, language, kind], queryFn: () => api<GraphData>(`/analyses/${enc(analysisId)}/graph?${new URLSearchParams({level, focus, direction, depth: '1', language, kind})}`)});
  useEffect(() => {
    if (!graph.data) return;
    setLayoutPending(true);
    const elk = new ELK({workerUrl: elkWorkerUrl});
    let active = true;
    const finish = (value: Record<string, {x: number; y: number}>) => {if (!active) return; setPositions(value); setTimeout(() => {if (active) void fitView({padding: 0.16, duration: 200, maxZoom: 1.05}).then(() => {if (active) setLayoutPending(false);});}, 100);};
    void elk.layout({id: 'root', layoutOptions: {
      'elk.algorithm': 'layered', 'elk.direction': level === 'modules' ? 'DOWN' : 'RIGHT', 'elk.spacing.nodeNode': '45',
      'elk.layered.spacing.nodeNodeBetweenLayers': '90', 'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
    }, children: graph.data.nodes.map(node => ({id: node.id, width: 226, height: 78})),
      edges: graph.data.edges.map(edge => ({id: edge.id, sources: [edge.source], targets: [edge.target]})),
    }).then(result => finish(Object.fromEntries((result.children || []).map(node => [node.id, {x: node.x || 0, y: node.y || 0}]))))
      .catch(() => finish(Object.fromEntries(graph.data!.nodes.map((node, index) => [node.id, {x: (index % 4) * 290, y: Math.floor(index / 4) * 125}]))));
    return () => {active = false; elk.terminateWorker();};
  }, [graph.data, fitView, level]);
  const nodes: FlowNode[] = useMemo(() => (graph.data?.nodes || []).map(node => ({id: node.id, type: 'code', position: positions[node.id] || {x: 0, y: 0}, data: {code: node, highlighted: highlights.includes(node.id), vertical: level === 'modules'}, selected: selected?.id === node.id})), [graph.data, positions, selected, highlights, level]);
  const edges = useMemo(() => (graph.data?.edges || []).map(edge => ({id: edge.id, source: edge.source, target: edge.target, type: 'smoothstep', animated: false,
    style: {stroke: edgeColors[edge.kind] || '#53818a', strokeWidth: selected && (edge.source === selected.id || edge.target === selected.id) ? 2.5 : 1.4, opacity: selected && edge.source !== selected.id && edge.target !== selected.id ? .3 : .8, strokeDasharray: edge.kind === 'contains' ? '4 5' : undefined},
    label: level === 'symbols' ? edge.kind : undefined, labelStyle: {fill: '#9ba7ad', fontSize: 10}, labelBgStyle: {fill: '#131c20', fillOpacity: .95}, markerEnd: edge.kind !== 'contains' ? {type: MarkerType.ArrowClosed, color: edgeColors[edge.kind], width: 13, height: 13} : undefined,
  })), [graph.data, selected, level]);
  return <div className="graph-shell">
    <div className="graph-toolbar"><div className="segmented" aria-label="Graph detail">{(['modules','files','symbols'] as GraphLevel[]).map(item => <button key={item} className={level === item ? 'active' : ''} onClick={() => setLevel(item)}>{item[0].toUpperCase()+item.slice(1)}</button>)}</div>
      <div className="toolbar-spacer"/><button title="Graph filters" aria-label="Graph filters" className={`icon-button ${filters ? 'active' : ''}`} onClick={() => setFilters(!filters)}><SlidersHorizontal size={15}/></button><button title="Reset graph view" aria-label="Reset graph view" className="icon-button" onClick={() => {setFocus(''); setDirection('both'); setLanguage(''); setKind(''); setLevel('modules'); void fitView({duration: 300});}}><RotateCcw size={15}/></button>
    </div>
    {filters && <div className="graph-filters"><label>Language<select value={language} onChange={event => setLanguage(event.target.value)}><option value="">All languages</option>{languages.map(item => <option key={item}>{item}</option>)}</select></label><label>Node type<select value={kind} onChange={event => setKind(event.target.value)}><option value="">All types</option>{['module','file','symbol','external'].map(item => <option key={item}>{item}</option>)}</select></label></div>}
    {focus && <div className="focus-chip"><GitFork size={12}/><span>{direction === 'both' ? 'Focused on' : direction === 'dependencies' ? 'Dependencies of' : 'Dependents of'} <b>{focus.replace(/^(file|module|symbol):/, '').split(':')[0]}</b></span><button onClick={() => {setFocus(''); setDirection('both');}} aria-label="Clear graph focus">×</button></div>}
    {graph.error ? <div className="center-state"><GitFork size={26}/><h3>Graph unavailable</h3><p>{graph.error.message}</p><button className="button secondary" onClick={() => void graph.refetch()}>Try again</button></div> : graph.isLoading ? <div className="center-state"><Loader2 className="spin" size={25}/><p>Loading graph…</p></div> : nodes.length === 0 ? <div className="center-state"><GitFork size={28}/><h3>No nodes in this view</h3><p>Try another detail level or clear the filters.</p></div> : <ReactFlow<FlowNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodeClick={(_, node) => onSelect(node.data.code)} onNodeDoubleClick={(_, node) => onDrill(node.data.code)} nodesDraggable={false} nodesConnectable={false} deleteKeyCode={null} minZoom={.12} maxZoom={2} fitView proOptions={{hideAttribution: true}} colorMode="dark" ariaLabelConfig={{'node.a11yDescription.default': 'Select a node to inspect its source. Double click to explore inside.'}}>
      <Background color="#2b373c" gap={22} size={1} variant={BackgroundVariant.Dots}/><Controls showInteractive={false}/><MiniMap nodeColor={node => node.selected ? '#75dfd0' : '#36525a'} maskColor="rgba(12, 18, 21, .7)" pannable zoomable/>
    </ReactFlow>}
    {layoutPending && !graph.isLoading && <div className="layout-indicator"><Loader2 size={12} className="spin"/> Arranging map</div>}
    <div className="graph-foot"><span><i className="legend-line"/>dependency</span><span><i className="legend-line contains"/>contains</span><span className="graph-count">{nodes.length} nodes · {edges.length} edges{(graph.data?.omittedNodes || 0) > 0 && ` · ${graph.data?.omittedNodes} more hidden`}</span></div>
  </div>;
}
export default function GraphView(props: Props) {return <ReactFlowProvider><GraphInner {...props}/></ReactFlowProvider>;}
