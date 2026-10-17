import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';
import { api, enc } from './api';
import { CommitHistory } from './CommitHistory';
import type { Analysis } from './types';

export function ComparePanel({analysis}: {analysis: Analysis}) {
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
      <span className="eyebrow">COMMIT COMPARISON</span>
      <h2>See what changed.</h2>
      <p>Choose a commit to see its actual file changes against <code>{analysis.commitSha.slice(0, 8)}</code>. Comparing reads GitHub directly and does not start another analysis.</p>
    </div>
    {previous.error && <p className="feature-error" role="alert">The linked revision could not be loaded. Choose a commit below to compare.</p>}
    {previous.data && previous.data.repositoryId !== analysis.repositoryId && <p className="feature-error" role="alert">The linked revision belongs to another repository. Choose a commit below to compare.</p>}
    {analysis.demo
      ? <div className="comparison-empty">This sample has no GitHub commit history. Open a public GitHub repository to compare its commits and inspect changed files here.</div>
      : <CommitHistory analysis={analysis} preferredSha={preferredSha} waitForPreferredSha={!!previousId && previous.isPending}/>}
  </div>;
}
