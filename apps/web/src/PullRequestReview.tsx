import { useState, type FormEvent } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { ArrowRight, ArrowUpRight, GitPullRequest, Loader2 } from 'lucide-react';
import { api, enc } from './api';
import type { Analysis, CommitChangedFile, IndexedPaths, PullDetail, PullFiles, PullList } from './types';

function storedNumber(analysisId: string): number | null {
  try {
    const value = Number(sessionStorage.getItem(`atlas-pull:${analysisId}`));
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  } catch { return null; }
}

export function PullRequestReview({analysis, onImpact}: {analysis: Analysis; onImpact: (path: string) => void}) {
  const [state, setState] = useState<'open' | 'closed'>(() => {
    try { return sessionStorage.getItem(`atlas-pull-state:${analysis.id}`) === 'closed' ? 'closed' : 'open'; }
    catch { return 'open'; }
  });
  const [numberDraft, setNumberDraft] = useState('');
  const [selectedNumber, setSelectedNumber] = useState<number | null>(() => storedNumber(analysis.id));
  const [pathFilter, setPathFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [viewed, setViewed] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(sessionStorage.getItem(`atlas-pull-viewed:${analysis.id}`) || '[]')); }
    catch { return new Set(); }
  });

  const select = (number: number) => {
    setSelectedNumber(number); setPathFilter(''); setStatusFilter('all');
    try { sessionStorage.setItem(`atlas-pull:${analysis.id}`, String(number)); } catch { /* Optional. */ }
    requestAnimationFrame(() => document.getElementById('pull-detail')?.scrollIntoView({behavior: 'smooth', block: 'start'}));
  };
  const chooseState = (next: 'open' | 'closed') => {
    setState(next);
    try { sessionStorage.setItem(`atlas-pull-state:${analysis.id}`, next); } catch { /* Optional. */ }
  };
  const lookup = (event: FormEvent) => {
    event.preventDefault();
    const number = Number(numberDraft.trim());
    if (Number.isSafeInteger(number) && number > 0) select(number);
  };
  const markViewed = (file: CommitChangedFile, headSha: string) => {
    const key = `${selectedNumber}:${headSha}:${file.filename}`;
    setViewed(previous => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key); else next.add(key);
      try { sessionStorage.setItem(`atlas-pull-viewed:${analysis.id}`, JSON.stringify([...next].slice(-1000))); } catch { /* Optional. */ }
      return next;
    });
  };

  const pulls = useInfiniteQuery({
    queryKey: ['pulls', analysis.id, state], initialPageParam: 1,
    queryFn: ({pageParam}) => api<PullList>(`/analyses/${enc(analysis.id)}/pulls?state=${state}&page=${pageParam}`),
    getNextPageParam: page => page.hasMore ? page.page + 1 : undefined,
    staleTime: 60_000,
  });
  const detail = useQuery({
    queryKey: ['pull-detail', analysis.id, selectedNumber],
    queryFn: () => api<PullDetail>(`/analyses/${enc(analysis.id)}/pulls/${selectedNumber}`),
    enabled: selectedNumber !== null, staleTime: 60_000, retry: false,
  });
  const filePages = useInfiniteQuery({
    queryKey: ['pull-files', analysis.id, selectedNumber, detail.data?.headSha], initialPageParam: 1,
    queryFn: ({pageParam}) => api<PullFiles>(`/analyses/${enc(analysis.id)}/pulls/${selectedNumber}/files?page=${pageParam}`),
    getNextPageParam: page => page.hasMore ? page.page + 1 : undefined,
    enabled: !!detail.data, staleTime: 60_000,
  });
  const indexed = useQuery({
    queryKey: ['indexed-paths', analysis.id],
    queryFn: () => api<IndexedPaths>(`/analyses/${enc(analysis.id)}/indexed-paths`),
    enabled: !!detail.data && analysis.graphReady, staleTime: 10 * 60_000,
  });

  const list = pulls.data?.pages.flatMap(page => page.pulls) || [];
  const files = filePages.data?.pages.flatMap(page => page.files) || [];
  const pr = detail.data;
  const mappedRevision = pr && indexed.data?.commitSha === pr.headSha ? 'head' : pr && indexed.data?.commitSha === pr.baseSha ? 'base' : null;
  const indexedPaths = new Set(indexed.data?.paths || []);
  const pathInMap = (file: CommitChangedFile) => mappedRevision === 'base' && file.status === 'renamed' ? file.previousFilename || file.filename : file.filename;
  const visibleFiles = files.filter(file => file.filename.toLowerCase().includes(pathFilter.toLowerCase().trim()) && (statusFilter === 'all' || file.status === statusFilter));
  const viewedCount = pr ? files.filter(file => viewed.has(`${pr.number}:${pr.headSha}:${file.filename}`)).length : 0;
  const date = (value: string) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleDateString() : '';

  return <section className="commit-history" aria-label="Pull request review">
    <div className="commit-history-heading"><div><h3><GitPullRequest size={20}/>Pull requests</h3><p>Review GitHub changes against a pull request’s base branch. Opening a PR fetches its metadata and file patches; it does not start indexing or use AI.</p></div>{!!list.length && <span>{list.length} loaded</span>}</div>
    <div className="pull-controls"><div className="commit-mode" role="group" aria-label="Pull request state"><button type="button" className={state === 'open' ? 'active' : ''} aria-pressed={state === 'open'} onClick={() => chooseState('open')}>Open</button><button type="button" className={state === 'closed' ? 'active' : ''} aria-pressed={state === 'closed'} onClick={() => chooseState('closed')}>Closed</button></div><form onSubmit={lookup}><label htmlFor="pull-number">PR number</label><input id="pull-number" type="number" min="1" step="1" value={numberDraft} onChange={event => setNumberDraft(event.target.value)} placeholder="e.g. 123"/><button className="button secondary" type="submit" disabled={!Number.isSafeInteger(Number(numberDraft)) || Number(numberDraft) < 1}>Open PR</button></form></div>
    {pulls.isPending && <div className="feature-loading"><Loader2 size={17} className="spin"/>Fetching pull requests…</div>}
    {pulls.error && <p className="feature-error" role="alert">{pulls.error.message} <button className="text-button" type="button" onClick={() => void pulls.refetch()}>Retry</button></p>}
    <div className="commit-list" aria-label={`${state} pull requests`}>{list.map(item => <div className={`commit-row ${selectedNumber === item.number ? 'selected' : ''}`} key={item.number}><div className="commit-row-copy"><strong title={item.title}>#{item.number} {item.title}</strong><small>{item.author} · updated {date(item.updatedAt)} · {item.baseRef} ← {item.headRef}{item.draft ? ' · draft' : ''}</small></div><button className="button secondary" type="button" aria-pressed={selectedNumber === item.number} onClick={() => select(item.number)}>{selectedNumber === item.number ? 'Viewing' : 'Review changes'}</button></div>)}</div>
    {pulls.isSuccess && !list.length && <p className="feature-empty">No {state} pull requests were found. Enter a PR number above to open one directly.</p>}
    {pulls.hasNextPage && <button className="commit-more button secondary" type="button" disabled={pulls.isFetchingNextPage} onClick={() => void pulls.fetchNextPage()}>{pulls.isFetchingNextPage && <Loader2 size={15} className="spin"/>}Load more pull requests</button>}

    {selectedNumber !== null && <div className="commit-detail" id="pull-detail" aria-live="polite">
      {detail.isPending && <div className="feature-loading"><Loader2 size={17} className="spin"/>Loading PR #{selectedNumber}…</div>}
      {detail.error && <p className="feature-error" role="alert">{detail.error.message} <button className="text-button" type="button" onClick={() => void detail.refetch()}>Retry</button></p>}
      {pr && <>
        <div className="commit-detail-heading"><div><span className="eyebrow">PR #{pr.number} · {pr.state.toUpperCase()}{pr.draft ? ' · DRAFT' : ''}</span><h4>{pr.title}</h4><p>{pr.author} · <code>{pr.baseRef}</code> <ArrowRight size={13}/> <code>{pr.headRef}</code> · {pr.changedFiles} changed files · +{pr.additions} / −{pr.deletions}</p></div><a href={pr.url} target="_blank" rel="noopener noreferrer">Open on GitHub <ArrowUpRight size={15}/></a></div>
        {indexed.isPending && <p className="commit-hint">Checking Atlas map coverage…</p>}
        {indexed.error && <p className="commit-hint">Map coverage is unavailable. GitHub changes remain visible.</p>}
        {indexed.data && filePages.data && <div className="commit-coverage">{mappedRevision ? <><strong>{files.filter(file => indexedPaths.has(pathInMap(file))).length} of {files.length} loaded changed files are in the {mappedRevision === 'base' ? 'base' : 'head'} Atlas snapshot.</strong><span>{mappedRevision === 'base' ? 'Impact shows existing dependents before this PR is applied.' : 'Impact shows dependents in the analyzed PR head.'} Atlas is pinned to <code>{analysis.commitSha.slice(0, 8)}</code>.</span></> : <><strong>Atlas is pinned to {analysis.commitSha.slice(0, 8)}.</strong><span>This PR is {pr.baseSha.slice(0, 8)} → {pr.headSha.slice(0, 8)}. Impact needs an Atlas map at its base or head; its GitHub diff is available below.</span></>}</div>}
        {filePages.isPending && <div className="feature-loading"><Loader2 size={17} className="spin"/>Loading changed files…</div>}
        {filePages.error && <p className="feature-error" role="alert">{filePages.error.message} <button className="text-button" type="button" onClick={() => void filePages.refetch()}>Retry</button></p>}
        {filePages.data && <>
          {!!files.length && <><div className="commit-file-tools"><label>Filter files<input value={pathFilter} onChange={event => setPathFilter(event.target.value)} placeholder="Path contains…"/></label><label>Change type<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">All changes</option>{[...new Set(files.map(file => file.status))].map(status => <option key={status} value={status}>{status}</option>)}</select></label><span>{viewedCount}/{files.length} viewed</span></div>
            {visibleFiles.map(file => { const mappedPath = pathInMap(file); const isMapped = !!mappedRevision && indexedPaths.has(mappedPath); return <details className="commit-file" key={file.filename}><summary><span className={`change-tag ${file.status}`}>{file.status}</span><code>{file.filename}</code><small>+{file.additions} / −{file.deletions}</small></summary><div className="commit-file-meta"><span className={isMapped ? 'mapped' : ''}>{isMapped ? `Mapped at PR ${mappedRevision}` : mappedRevision ? 'GitHub only · outside this map' : `GitHub diff · map at ${analysis.commitSha.slice(0, 8)}`}</span><label><input type="checkbox" checked={viewed.has(`${pr.number}:${pr.headSha}:${file.filename}`)} onChange={() => markViewed(file, pr.headSha)}/>Viewed</label>{isMapped && <button className="text-button" type="button" onClick={() => onImpact(mappedPath)}>Explore {mappedRevision === 'base' ? 'baseline ' : ''}impact <ArrowRight size={14}/></button>}</div>{file.previousFilename && <p>Previously {file.previousFilename}</p>}{file.redacted && <p>Sensitive values are redacted.</p>}{file.patch ? <pre aria-label={`Changes in ${file.filename}`}>{file.patch.split('\n').map((line, index) => <span className={line.startsWith('+') ? 'diff-added' : line.startsWith('-') ? 'diff-removed' : line.startsWith('@@') ? 'diff-location' : ''} key={index}>{line || ' '}</span>)}</pre> : <p>No text patch is available here. Open this change on GitHub to inspect it.</p>}{file.patchTruncated && <p>The patch is shortened here. Open the change on GitHub for its complete diff.</p>}</details>; })}
            {!visibleFiles.length && <p className="feature-empty">No loaded files match these filters.</p>}</>}
          {!files.length && <p className="feature-empty">GitHub reports no changed files for this pull request.</p>}
          {filePages.hasNextPage && <button className="commit-more button secondary" type="button" disabled={filePages.isFetchingNextPage} onClick={() => void filePages.fetchNextPage()}>{filePages.isFetchingNextPage && <Loader2 size={15} className="spin"/>}Load more changed files</button>}
          {pr.changedFiles > 3000 && files.length >= 3000 && <p className="commit-hint">GitHub limits this endpoint to 3,000 files. Open the PR on GitHub for the remaining changes.</p>}
        </>}
      </>}
    </div>}
  </section>;
}
