import { useEffect, useState, type FormEvent } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { ArrowRight, ArrowUpRight, GitCommitHorizontal, Loader2 } from 'lucide-react';
import { api, enc } from './api';
import type { Analysis, CommitChangedFile, CommitComparison, CommitDetail, CommitList, IndexedPaths } from './types';

type Mode = 'commit' | 'range';
type CommitSession = {mode: Mode; selectedSha: string; historyRef: string; viewed: string[]};

function readCommitSession(analysisId: string): CommitSession | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(`atlas-commits:${analysisId}`) || 'null');
    if (value && (value.mode === 'commit' || value.mode === 'range') && typeof value.selectedSha === 'string' && typeof value.historyRef === 'string' && Array.isArray(value.viewed)) return value;
  } catch { /* Browser storage is optional. */ }
  return null;
}

export function CommitHistory({analysis, preferredSha = '', onImpact}: {analysis: Analysis; preferredSha?: string; onImpact: (path: string) => void}) {
  const [saved] = useState(() => readCommitSession(analysis.id));
  const initialPreferred = preferredSha !== analysis.commitSha ? preferredSha : '';
  const [mode, setMode] = useState<Mode>(initialPreferred ? 'range' : saved?.mode || 'commit');
  const [selectedSha, setSelectedSha] = useState(initialPreferred || saved?.selectedSha || '');
  const [manuallySelected, setManuallySelected] = useState(false);
  const [historyRef, setHistoryRef] = useState(saved?.historyRef || '');
  const [refDraft, setRefDraft] = useState(saved?.historyRef || '');
  const [pathFilter, setPathFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [viewed, setViewed] = useState<Set<string>>(() => new Set(saved?.viewed || []));

  useEffect(() => { if (preferredSha && preferredSha !== analysis.commitSha && !manuallySelected) { setSelectedSha(preferredSha); setMode('range'); } }, [preferredSha, analysis.commitSha, manuallySelected]);
  useEffect(() => {
    try { sessionStorage.setItem(`atlas-commits:${analysis.id}`, JSON.stringify({mode, selectedSha, historyRef, viewed: [...viewed].slice(-1000)})); } catch { /* Browser storage is optional. */ }
  }, [analysis.id, mode, selectedSha, historyRef, viewed]);
  const history = useInfiniteQuery({
    queryKey: ['commits', analysis.id, historyRef], initialPageParam: 1,
    queryFn: ({pageParam}) => api<CommitList>(`/analyses/${enc(analysis.id)}/commits?page=${pageParam}${historyRef ? `&ref=${enc(historyRef)}` : ''}`),
    getNextPageParam: page => page.hasMore ? page.page + 1 : undefined,
    enabled: !analysis.demo, staleTime: 60_000,
  });
  const commitDetail = useInfiniteQuery({
    queryKey: ['commit-detail', analysis.id, selectedSha], initialPageParam: 1,
    queryFn: ({pageParam}) => api<CommitDetail>(`/analyses/${enc(analysis.id)}/commits/${enc(selectedSha)}?page=${pageParam}`),
    getNextPageParam: page => page.hasMore ? page.page + 1 : undefined,
    enabled: !!selectedSha && mode === 'commit' && !analysis.demo, staleTime: 5 * 60_000,
  });
  const comparison = useQuery({
    queryKey: ['github-comparison', analysis.id, selectedSha],
    queryFn: () => api<CommitComparison>(`/analyses/${enc(analysis.id)}/compare/commits?selected=${enc(selectedSha)}`),
    enabled: !!selectedSha && selectedSha !== analysis.commitSha && mode === 'range' && !analysis.demo,
    staleTime: 5 * 60_000,
  });
  const indexed = useQuery({
    queryKey: ['indexed-paths', analysis.id],
    queryFn: () => api<IndexedPaths>(`/analyses/${enc(analysis.id)}/indexed-paths`),
    enabled: !!selectedSha && analysis.graphReady && !analysis.demo,
    staleTime: 10 * 60_000,
  });

  const commits = (history.data?.pages.flatMap(page => page.commits) || []).sort((a, b) => (Date.parse(b.committedAt) || 0) - (Date.parse(a.committedAt) || 0));
  const selectedIndex = commits.findIndex(commit => commit.sha === selectedSha);
  const single = commitDetail.data?.pages[0];
  const range = comparison.data;
  const files = mode === 'commit' ? commitDetail.data?.pages.flatMap(page => page.files) || [] : range?.files || [];
  const targetSha = mode === 'commit' ? selectedSha : range?.headSha;
  const mapMatches = !!targetSha && indexed.data?.commitSha === targetSha;
  const indexedPaths = new Set(indexed.data?.paths || []);
  const visibleFiles = files.filter(file => file.filename.toLowerCase().includes(pathFilter.toLowerCase().trim()) && (statusFilter === 'all' || file.status === statusFilter));
  const viewKey = `${mode}:${mode === 'range' ? `${range?.baseSha}:${range?.headSha}` : selectedSha}`;
  const viewedCount = files.filter(file => viewed.has(`${viewKey}:${file.filename}`)).length;
  const date = (value: string) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleDateString() : '';

  const choose = (sha: string) => {
    setManuallySelected(true);
    if (sha === analysis.commitSha) setMode('commit');
    setSelectedSha(sha); setPathFilter(''); setStatusFilter('all');
    requestAnimationFrame(() => document.getElementById('commit-detail')?.scrollIntoView({behavior: 'smooth', block: 'start'}));
  };
  const applyRef = (event: FormEvent) => {
    event.preventDefault(); const next = refDraft.trim();
    if (next === historyRef) return;
    setManuallySelected(true);
    setHistoryRef(next); setSelectedSha(''); setMode('commit'); setPathFilter(''); setStatusFilter('all');
  };
  const markViewed = (file: CommitChangedFile) => {
    const key = `${viewKey}:${file.filename}`;
    setViewed(previous => { const next = new Set(previous); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  };

  if (analysis.demo) return null;
  const branch = history.data?.pages[0]?.branch;
  const loadingDetail = mode === 'commit' ? commitDetail.isPending : comparison.isPending;
  const detailError = mode === 'commit' ? commitDetail.error : comparison.error;
  const detailReady = mode === 'commit' ? !!single : !!range;
  const detailUrl = mode === 'commit' ? single?.url : range?.url;

  return <section className="commit-history" aria-label="Commit explorer">
    <div className="commit-history-heading"><div><h3><GitCommitHorizontal size={20}/>Commit explorer</h3><p>Browse one commit at a time, or compare a commit with the pinned snapshot. GitHub provides the changes; browsing does not start indexing or use AI.</p></div>{!!commits.length && <span>{commits.length} loaded</span>}</div>
    <form className="commit-ref-form" onSubmit={applyRef}><label htmlFor="commit-ref">History from branch or SHA</label><div><input id="commit-ref" value={refDraft} onChange={event => setRefDraft(event.target.value)} placeholder={branch || 'Default branch'} maxLength={256}/><button className="button secondary" type="submit">Load history</button></div><small>Showing {branch ? <code>{branch}</code> : 'the default branch'} · older commits load on demand</small></form>
    {history.isPending && <div className="feature-loading"><Loader2 size={17} className="spin"/>Fetching commits…</div>}
    {history.error && <p className="feature-error" role="alert">{history.error.message}</p>}
    <div className="commit-list" aria-label="Loaded commits">{commits.map(commit => <div className={`commit-row ${selectedSha === commit.sha ? 'selected' : ''}`} key={commit.sha}>
      <div className="commit-row-copy"><strong title={commit.message}>{commit.message}</strong><small>{commit.author} · {date(commit.committedAt)} · <code>{commit.sha.slice(0, 8)}</code>{commit.sha === analysis.commitSha ? ' · pinned snapshot' : ''}</small></div>
      <button className="button secondary" type="button" aria-pressed={selectedSha === commit.sha} onClick={() => choose(commit.sha)}>{selectedSha === commit.sha ? 'Viewing' : 'View changes'}</button>
    </div>)}</div>
    {history.isSuccess && !commits.length && <p className="feature-empty">No commits were found for this branch or SHA.</p>}
    {history.hasNextPage && <button className="commit-more button secondary" type="button" disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage && <Loader2 size={15} className="spin"/>}Load older commits</button>}

    {selectedSha && <div className="commit-detail" id="commit-detail" aria-live="polite">
      <div className="commit-mode" role="group" aria-label="Change view"><button type="button" className={mode === 'commit' ? 'active' : ''} aria-pressed={mode === 'commit'} onClick={() => setMode('commit')}>This commit</button><button type="button" className={mode === 'range' ? 'active' : ''} aria-pressed={mode === 'range'} disabled={selectedSha === analysis.commitSha} onClick={() => setMode('range')}>Since this commit</button></div>
      <p className="commit-mode-help">{mode === 'commit' ? 'GitHub’s file diff for this commit.' : `Cumulative changes between this commit and snapshot ${analysis.commitSha.slice(0, 8)}.`}</p>
      {loadingDetail && <div className="feature-loading"><Loader2 size={17} className="spin"/>Loading changes from GitHub…</div>}
      {detailError && <p className="feature-error" role="alert">{detailError.message}</p>}
      {detailReady && <>
        <div className="commit-detail-heading"><div><span className="eyebrow">{mode === 'commit' ? 'SINGLE COMMIT' : 'REVISION RANGE'}</span><h4>{files.length}{mode === 'commit' && commitDetail.hasNextPage ? '+' : ''} changed {files.length === 1 ? 'file' : 'files'}{mode === 'commit' && commitDetail.hasNextPage ? ' loaded' : ''}</h4><p>{mode === 'commit' ? <><code>{selectedSha.slice(0, 8)}</code> · {single?.message}</> : <><code>{range?.baseSha.slice(0, 8)}</code><ArrowRight size={13}/><code>{range?.headSha.slice(0, 8)}</code>{range?.status === 'diverged' ? ' · divergent history; review the base and head on GitHub' : ''}</>}</p></div>{detailUrl && <a href={detailUrl} target="_blank" rel="noopener noreferrer">Open on GitHub <ArrowUpRight size={15}/></a>}</div>
        {indexed.isPending && <p className="commit-hint">Checking Atlas map coverage…</p>}
        {indexed.error && <p className="commit-hint">Map coverage is unavailable. GitHub changes remain visible.</p>}
        {indexed.data && <div className="commit-coverage">{mapMatches ? <><strong>{files.filter(file => indexedPaths.has(file.filename)).length} of {files.length} {mode === 'commit' && commitDetail.hasNextPage ? 'loaded ' : ''}changed files are in this Atlas snapshot.</strong><span>Impact uses static dependencies at <code>{targetSha?.slice(0, 8)}</code>. GitHub-only files remain visible below.</span></> : <><strong>Atlas map is pinned to {analysis.commitSha.slice(0, 8)}.</strong><span>This view ends at {targetSha?.slice(0, 8)}. Impact is available when the selected changes end at the mapped revision.</span></>}</div>}
        {!!files.length && <><div className="commit-file-tools"><label>Filter files<input value={pathFilter} onChange={event => setPathFilter(event.target.value)} placeholder="Path contains…"/></label><label>Change type<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">All changes</option>{[...new Set(files.map(file => file.status))].map(status => <option key={status} value={status}>{status}</option>)}</select></label><span>{viewedCount}/{files.length} viewed</span></div>
          {visibleFiles.map(file => <details className="commit-file" key={file.filename}><summary><span className={`change-tag ${file.status}`}>{file.status}</span><code>{file.filename}</code><small>+{file.additions} / −{file.deletions}</small></summary><div className="commit-file-meta"><span className={mapMatches && indexedPaths.has(file.filename) ? 'mapped' : ''}>{mapMatches ? indexedPaths.has(file.filename) ? 'Mapped by Atlas' : 'GitHub only · outside this map' : `GitHub diff · map at ${analysis.commitSha.slice(0, 8)}`}</span><label><input type="checkbox" checked={viewed.has(`${viewKey}:${file.filename}`)} onChange={() => markViewed(file)}/>Viewed</label>{mapMatches && indexedPaths.has(file.filename) && <button className="text-button" type="button" onClick={() => onImpact(file.filename)}>Explore impact <ArrowRight size={14}/></button>}</div>{file.previousFilename && <p>Previously {file.previousFilename}</p>}{file.redacted && <p>Sensitive values are redacted.</p>}{file.patch ? <pre aria-label={`Changes in ${file.filename}`}>{file.patch.split('\n').map((line, index) => <span className={line.startsWith('+') ? 'diff-added' : line.startsWith('-') ? 'diff-removed' : line.startsWith('@@') ? 'diff-location' : ''} key={index}>{line || ' '}</span>)}</pre> : <p>No text patch is available here. Open this change on GitHub to inspect it.</p>}{file.patchTruncated && <p>The patch is shortened here. Open the change on GitHub for its complete diff.</p>}</details>)}
          {!visibleFiles.length && <p className="feature-empty">No loaded files match these filters.</p>}
        </>}
        {!files.length && <p className="feature-empty">GitHub reports no changed files for this {mode === 'commit' ? 'commit' : 'range'}.</p>}
        {mode === 'commit' && commitDetail.hasNextPage && <button className="commit-more button secondary" type="button" disabled={commitDetail.isFetchingNextPage} onClick={() => void commitDetail.fetchNextPage()}>{commitDetail.isFetchingNextPage && <Loader2 size={15} className="spin"/>}Load more changed files</button>}
        {mode === 'range' && range?.possiblyTruncated && <p className="commit-hint">GitHub limits this range to 300 changed files. Open the full comparison on GitHub.</p>}
      </>}
      {selectedIndex >= 0 && <div className="commit-navigation"><button className="button secondary" type="button" disabled={selectedIndex === 0} onClick={() => choose(commits[selectedIndex - 1].sha)}>Newer commit</button><button className="button secondary" type="button" disabled={selectedIndex >= commits.length - 1} onClick={() => choose(commits[selectedIndex + 1].sha)}>Older commit</button></div>}
    </div>}
  </section>;
}
