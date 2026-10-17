import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';
import { api, enc } from './api';
import { CommitHistory } from './CommitHistory';
import { PullRequestReview } from './PullRequestReview';
import type { Analysis } from './types';

export function ComparePanel({analysis, onImpact}: {analysis: Analysis; onImpact: (path: string) => void}) {
  const [view, setView] = useState<'commits' | 'pulls'>(() => {
    try { return sessionStorage.getItem(`atlas-change-view:${analysis.id}`) === 'pulls' ? 'pulls' : 'commits'; }
    catch { return 'commits'; }
  });
  const chooseView = (next: 'commits' | 'pulls') => {
    setView(next);
    try { sessionStorage.setItem(`atlas-change-view:${analysis.id}`, next); } catch { /* Optional. */ }
  };
  const location = useLocation();
  const previousId = new URLSearchParams(location.search).get('compare') || '';
  const previous = useQuery({
    queryKey: ['comparison-revision', previousId],
    queryFn: () => api<Analysis>(`/analyses/${enc(previousId)}`),
    enabled: !!previousId && !analysis.demo,
    retry: false,
  });
  const preferredSha = previous.data?.repositoryId === analysis.repositoryId ? previous.data.commitSha : '';

  return <div className="feature-content">
    <div className="feature-title">
      <span className="eyebrow">CHANGE EXPLORER</span>
      <h2>Follow the changes.</h2>
      <p>Inspect commits and pull requests alongside snapshot <code>{analysis.commitSha.slice(0, 8)}</code>. Browsing uses GitHub data and does not start another analysis.</p>
    </div>
    {previous.error && <p className="feature-error" role="alert">The linked revision could not be loaded. Choose a commit below to compare.</p>}
    {previous.data && previous.data.repositoryId !== analysis.repositoryId && <p className="feature-error" role="alert">The linked revision belongs to another repository. Choose a commit below to compare.</p>}
    {!analysis.demo && <div className="change-view-tabs" role="group" aria-label="Change explorer view"><button type="button" className={view === 'commits' ? 'active' : ''} aria-pressed={view === 'commits'} onClick={() => chooseView('commits')}>Commits</button><button type="button" className={view === 'pulls' ? 'active' : ''} aria-pressed={view === 'pulls'} onClick={() => chooseView('pulls')}>Pull requests</button></div>}
    {analysis.demo
      ? <div className="comparison-empty">This sample has no GitHub commit history. Open a public GitHub repository to compare its commits and inspect changed files here.</div>
      : view === 'commits' ? <CommitHistory analysis={analysis} preferredSha={preferredSha} onImpact={onImpact}/>
        : <PullRequestReview analysis={analysis} onImpact={onImpact}/>}
  </div>;
}
