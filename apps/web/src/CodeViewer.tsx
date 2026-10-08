import { useEffect, useRef } from 'react';
import Editor, { loader, type OnMount } from '@monaco-editor/react';
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api';
import 'monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution';
import 'monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution';
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution';
import 'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution';
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker';
import { useQuery } from '@tanstack/react-query';
import { ArrowDownLeft, ArrowUpRight, Braces, FileCode2, Loader2, Sparkles, X } from 'lucide-react';
import { api, enc } from './api';
import type { CodeNode, Direction, SourceFile } from './types';

// Bundle the editor locally: source browsing also works without a CDN connection.
self.MonacoEnvironment = {getWorker: () => new EditorWorker()};
loader.config({monaco});
// Source snapshots need highlighting; repository code never enters language services.
monaco.languages.register({id: 'json'});
monaco.languages.setMonarchTokensProvider('json', {tokenizer: {root: [
  [/"(?:[^"\\]|\\.)*"(?=\s*:)/, 'key'], [/"(?:[^"\\]|\\.)*"/, 'string'],
  [/\b(?:true|false|null)\b/, 'keyword'], [/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, 'number'],
]}});
monaco.editor.defineTheme('atlas', {base: 'vs-dark', inherit: true, rules: [
  {token: 'comment', foreground: '657b81', fontStyle: 'italic'}, {token: 'keyword', foreground: 'b0a2de'}, {token: 'string', foreground: 'a9c68c'}, {token: 'number', foreground: 'd3a86a'}, {token: 'type.identifier', foreground: '87c8cd'},
], colors: {'editor.background': '#11191d', 'editor.foreground': '#c4cfd4', 'editorLineNumber.foreground': '#42535c', 'editorLineNumber.activeForeground': '#a5bbbF', 'editor.selectionBackground': '#2b4a51', 'editor.lineHighlightBackground': '#182328', 'editorCursor.foreground': '#8ddfd0', 'editorWidget.background': '#1c2a30'}});

export default function CodeViewer({analysisId, selected, lines, onClose, onExplain, onDependencies}: {analysisId: string; selected: CodeNode; lines?: {start: number; end: number}; onClose: () => void; onExplain: () => void; onDependencies: (direction: Direction) => void}) {
  const fileId = selected.fileId || (selected.kind === 'file' ? selected.id : '');
  const source = useQuery({queryKey: ['source', analysisId, fileId], queryFn: () => api<SourceFile>(`/analyses/${enc(analysisId)}/files/${enc(fileId)}`), enabled: !!fileId, staleTime: Infinity});
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const decorations = useRef<monaco.editor.IEditorDecorationsCollection | null>(null);
  const mark = () => {if (!editor.current || !source.data) return; const start = lines?.start || selected.startLine; const end = lines?.end || selected.endLine || start; decorations.current?.clear(); if (start) {editor.current.revealLineInCenter(start); decorations.current = editor.current.createDecorationsCollection([{range: new monaco.Range(start, 1, end || start, 1), options: {isWholeLine: true, className: 'source-highlight', linesDecorationsClassName: 'source-highlight-gutter'}}]);}};
  useEffect(() => {mark();}, [lines, selected.id, source.data]);
  const onMount: OnMount = mounted => {editor.current = mounted; mark();};
  return <div className="source-panel"><div className="source-toolbar"><FileCode2 size={14}/><span className="source-path" title={source.data?.path}>{source.data?.path || selected.path || selected.name}</span>{lines && <span className="line-badge">L{lines.start}–{lines.end}</span>}<div className="toolbar-spacer"/><button className="source-action" title="Show dependencies" aria-label="Show dependencies" onClick={() => onDependencies('dependencies')}><ArrowUpRight size={14}/><span>Uses</span></button><button className="source-action" title="Show dependents" aria-label="Show dependents" onClick={() => onDependencies('dependents')}><ArrowDownLeft size={14}/><span>Used by</span></button><button className="source-action explain" onClick={onExplain}><Sparkles size={13}/><span>Explain</span></button><button className="icon-button" aria-label="Close source viewer" onClick={onClose}><X size={15}/></button></div>
    {selected.kind === 'symbol' && <div className="symbol-strip"><Braces size={12}/><span>{selected.signature || selected.name}</span></div>}
    {source.data?.redacted && <div className="source-redacted">Sensitive values have been redacted. Line numbers are preserved.</div>}
    <div className="editor-container">{source.isLoading ? <div className="center-state"><Loader2 className="spin" size={21}/><p>Opening source…</p></div> : source.error ? <div className="center-state"><p>{source.error.message}</p></div> : <Editor path={`${analysisId}/${fileId}`} value={source.data?.content || ''} language={['typescript','tsx','ts'].includes(source.data?.language || '') ? 'typescript' : ['javascript','jsx','js'].includes(source.data?.language || '') ? 'javascript' : source.data?.language} theme="atlas" onMount={onMount} loading={<span className="muted">Loading editor…</span>} options={{readOnly: true, domReadOnly: true, minimap: {enabled: false}, fontSize: 12, fontFamily: '"Cascadia Code", "SFMono-Regular", Consolas, monospace', lineHeight: 21, lineNumbers: 'on', scrollBeyondLastLine: false, renderLineHighlight: 'all', folding: true, glyphMargin: false, padding: {top: 14}, automaticLayout: true, wordWrap: 'off', overviewRulerLanes: 0, scrollbar: {verticalScrollbarSize: 8, horizontalScrollbarSize: 8}}}/>}</div>
    <div className="source-footer"><span>{source.data?.language || 'source'}</span><span>{source.data?.lineCount || 0} lines</span><span>Read only</span></div>
  </div>;
}
