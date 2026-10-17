import { useQuery } from '@tanstack/react-query';
import { ArrowRight, FileCode2, GitBranch } from 'lucide-react';
import { api, enc } from './api';
import type { Analysis, CodeNode, GraphData } from './types';

export function ImpactSuggestions({analysis, onChoose}: {analysis: Analysis; onChoose: (node: CodeNode) => void}) {
  const graph = useQuery({
    queryKey: ['impact-suggestions', analysis.id],
    queryFn: () => api<GraphData>(`/analyses/${enc(analysis.id)}/graph?level=files&limit=1000`),
    staleTime: 5 * 60_000,
  });
  const entryPaths = analysis.overview?.entryPoints || [];
  const files = graph.data?.nodes.filter(node => node.kind === 'file') || [];
  const dependents = new Map<string, number>();
  for (const edge of graph.data?.edges || []) {
    if (edge.kind === 'imports' || edge.kind === 'reexports') dependents.set(edge.target, (dependents.get(edge.target) || 0) + 1);
  }
  const ranked = [...files].sort((a, b) => (dependents.get(b.id) || 0) - (dependents.get(a.id) || 0));
  const entry = files.find(node => entryPaths.includes(node.path || ''));
  const suggested = [...new Map([...ranked.filter(node => dependents.has(node.id)).slice(0, 3), ...(entry ? [entry] : []), ...ranked].map(node => [node.id, node])).values()].slice(0, 4);

  return <section className="impact-start" aria-label="Suggested files for impact analysis">
    <div className="impact-start-heading"><GitBranch size={27}/><div><h3>Pick a starting point</h3><p>Choose a file to see what depends on it. You can also search above for any file or function.</p></div></div>
    {graph.isLoading && <p className="feature-empty">Finding files to explore…</p>}
    {graph.error && <p className="feature-error" role="alert">Could not load suggestions. Search for a file above.</p>}
    {!!suggested.length && <><span className="impact-start-label">SUGGESTED FILES</span><div className="impact-start-list">{suggested.map(node => <button key={node.id} type="button" onClick={() => onChoose(node)}><FileCode2 size={18}/><span><strong>{node.name}</strong><small>{node.path} · {dependents.get(node.id) ? `${dependents.get(node.id)} incoming links` : 'Explore this file'}</small></span><ArrowRight size={17}/></button>)}</div></>}
  </section>;
}
