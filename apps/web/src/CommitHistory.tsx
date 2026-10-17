import { useEffect, useState } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { ArrowRight, ArrowUpRight, GitCommitHorizontal, Loader2 } from 'lucide-react';
import { api, enc } from './api';
import type { Analysis, CommitComparison, CommitList } from './types';

export function CommitHistory({analysis, preferredSha = '', waitForPreferredSha = false}: {analysis: Analysis; preferredSha?: string; waitForPreferredSha?: boolean}) {
  const [selectedSha, setSelectedSha] = useState('');
  const [manuallySelected, setManuallySelected] = useState(false);
  const history = useInfiniteQuery({
    queryKey: ['commits', analysis.id], initialPageParam: 1,
    queryFn: ({pageParam}) => api<CommitList>(`/analyses/${enc(analysis.id)}/commits?page=${pageParam}`),
    getNextPageParam: page => page.hasMore ? page.page + 1 : undefined,
    enabled: !analysis.demo, staleTime: 60_000,
  });
  const comparison = useQuery({
    queryKey: ['github-comparison', analysis.id, selectedSha],
    queryFn: () => api<CommitComparison>(`/analyses/${enc(analysis.id)}/compare/commits?selected=${enc(selectedSha)}`),
    enabled: !!selectedSha && !analysis.demo, staleTime: 5 * 60_000,
  });
  const commits = (history.data?.pages.flatMap(page => page.commits) || []).sort((a, b) => (Date.parse(b.committedAt) || 0) - (Date.parse(a.committedAt) || 0));
  const firstOtherSha = commits.find(commit => commit.sha !== analysis.commitSha)?.sha || '';
  useEffect(() => {if (preferredSha && preferredSha !== analysis.commitSha && !manuallySelected) setSelectedSha(preferredSha);}, [preferredSha, analysis.commitSha, manuallySelected]);
  useEffect(() => {if (!selectedSha && !waitForPreferredSha && firstOtherSha) setSelectedSha(firstOtherSha);}, [selectedSha, waitForPreferredSha, firstOtherSha]);
  if (analysis.demo) return null;
  const branch = history.data?.pages[0]?.branch;
  const result = comparison.data;
  const date = (value: string) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleDateString() : '';
  const select = (sha: string) => {setManuallySelected(true); setSelectedSha(sha); requestAnimationFrame(() => document.getElementById('commit-comparison')?.scrollIntoView({behavior: 'smooth', block: 'start'}));};

  return <section className="commit-history" aria-label="Commit comparison">
    <div className="commit-history-heading"><div><h3><GitCommitHorizontal size={20}/>Compare commits</h3><p>Choose a commit on {branch ? <code>{branch}</code> : 'the default branch'} to compare its files with the current snapshot. This reads GitHub changes directly; it does not create chunks or start a new analysis.</p></div>{!!commits.length && <span>{commits.length} loaded</span>}</div>
    {history.isPending && <div className="feature-loading"><Loader2 size={17} className="spin"/>Fetching commits…</div>}
    {history.error && <p className="feature-error" role="alert">{history.error.message}</p>}
    {selectedSha && <div className="commit-detail" id="commit-comparison" aria-live="polite">
      {comparison.isPending && <div className="feature-loading"><Loader2 size={17} className="spin"/>Comparing commits on GitHub…</div>}
      {comparison.error && <p className="feature-error" role="alert">{comparison.error.message}</p>}
      {result && <><div className="commit-detail-heading"><div><span className="eyebrow">GITHUB FILE COMPARISON</span><h4>{result.files.length} changed {result.files.length === 1 ? 'file' : 'files'}</h4><p><code>{result.baseSha.slice(0, 8)}</code> <ArrowRight size={13}/> <code>{result.headSha.slice(0, 8)}</code> · older to newer · all file types GitHub reports</p></div><a href={result.url} target="_blank" rel="noopener noreferrer">Open full comparison <ArrowUpRight size={15}/></a></div>
        {result.files.length ? result.files.map(file => <details className="commit-file" key={file.filename}><summary><span className={`change-tag ${file.status}`}>{file.status}</span><code>{file.filename}</code><small>+{file.additions} / −{file.deletions}</small></summary>{file.previousFilename && <p>Previously {file.previousFilename}</p>}{file.redacted && <p>Sensitive values are redacted.</p>}{file.patch ? <pre aria-label={`Changes in ${file.filename}`}>{file.patch.split('\n').map((line, index) => <span className={line.startsWith('+') ? 'diff-added' : line.startsWith('-') ? 'diff-removed' : line.startsWith('@@') ? 'diff-location' : ''} key={index}>{line || ' '}</span>)}</pre> : <p>No text patch is available for this file. Open the comparison on GitHub to inspect it.</p>}{file.patchTruncated && <p>The patch is shortened here. Open the comparison on GitHub for the complete change.</p>}</details>) : <p className="feature-empty">These commits have no file changes in GitHub’s comparison.</p>}
        {result.possiblyTruncated && <p className="commit-hint">GitHub limits this comparison to 300 changed files. Open it on GitHub for more context.</p>}
      </>}
    </div>}
    {!!commits.length && <div className="commit-list">{commits.map(commit => <div className={`commit-row ${selectedSha === commit.sha ? 'selected' : ''}`} key={commit.sha}>
      <div className="commit-row-copy"><strong title={commit.message}>{commit.message}</strong><small>{commit.author} · {date(commit.committedAt)} · <code>{commit.sha.slice(0, 8)}</code></small></div>
      {commit.sha === analysis.commitSha ? <span className="commit-current">Current snapshot</span> : <button className="button secondary" type="button" aria-pressed={selectedSha === commit.sha} onClick={() => select(commit.sha)}>{selectedSha === commit.sha ? 'Comparing' : 'Compare now'}</button>}
    </div>)}</div>}
    {history.isSuccess && !commits.length && <p className="feature-empty">No commits were found on this branch.</p>}
    {history.hasNextPage && <button className="commit-more button secondary" type="button" disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage && <Loader2 size={15} className="spin"/>}Load older commits</button>}
  </section>;
}
