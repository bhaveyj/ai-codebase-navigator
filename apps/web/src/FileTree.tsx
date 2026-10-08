import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, FileCode2, Folder, FolderOpen, Loader2 } from 'lucide-react';
import { api, enc } from './api';
import type { CodeNode, TreeEntry } from './types';

function Branch({analysisId, path = '', depth = 0, selected, onSelect}: {analysisId: string; path?: string; depth?: number; selected?: string; onSelect: (node: CodeNode) => void}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const tree = useQuery({queryKey: ['tree', analysisId, path], queryFn: () => api<{entries: TreeEntry[]}>(`/analyses/${enc(analysisId)}/tree?parent=${enc(path)}`)});
  if (tree.isLoading) return <div className="tree-loading" style={{paddingLeft: 16 + depth * 14}}><Loader2 size={13} className="spin"/> Loading…</div>;
  if (tree.error) return <p className="tree-error">{tree.error.message}</p>;
  return <>{tree.data?.entries.map(entry => <div key={entry.id}><button className={`tree-item ${selected === entry.id || selected === `file:${entry.path}` ? 'selected' : ''}`} style={{paddingLeft: 13 + depth * 15}} title={entry.path} onClick={() => {
    if (entry.kind === 'directory') setExpanded(current => {const next = new Set(current); if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path); return next;});
    else onSelect({id: entry.id, fileId: entry.id, path: entry.path, name: entry.name, language: entry.language, kind: 'file'});
  }}>{entry.kind === 'directory' ? <>{expanded.has(entry.path) ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}<span className="folder-icon">{expanded.has(entry.path) ? <FolderOpen size={14}/> : <Folder size={14}/>}</span></> : <><span className="tree-indent"/><FileCode2 size={14} className={`file-icon lang-${entry.language}`}/></>}<span>{entry.name}</span></button>{entry.kind === 'directory' && expanded.has(entry.path) && <Branch analysisId={analysisId} path={entry.path} depth={depth+1} selected={selected} onSelect={onSelect}/>}</div>)}</>;
}
export default function FileTree(props: {analysisId: string; selected?: string; onSelect: (node: CodeNode) => void}) {return <div className="file-tree" role="navigation" aria-label="Repository files"><Branch {...props}/></div>;}
