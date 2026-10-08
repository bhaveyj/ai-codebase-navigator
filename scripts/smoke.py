"""Exercise the running stack using real analysis and optional grounded RAG.

python scripts/smoke.py --base http://localhost:8080 --demo --rag
python scripts/smoke.py --url https://github.com/owner/repo
"""
from __future__ import annotations

import argparse
import json
import time
from urllib.parse import quote

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://localhost:8080')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--url')
    parser.add_argument('--rag', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if not args.demo and not args.url:
        parser.error('Choose --demo or --url.')
    with httpx.Client(base_url=args.base + '/api/v1', timeout=300) as client:
        health = client.get('/health/ready')
        health.raise_for_status()
        response = client.post('/demo' if args.demo else '/repositories', json={} if args.demo else {'url': args.url})
        response.raise_for_status()
        data = response.json()
        analysis_id = data['analysis']['id']
        job_id = data['job']['id']
        print(json.dumps({'analysisId': analysis_id, 'jobId': job_id}), flush=True)
        deadline = time.monotonic() + args.timeout
        last_phase = None
        while time.monotonic() < deadline:
            response = client.get(f'/analyses/{analysis_id}')
            response.raise_for_status()
            analysis = response.json()
            if analysis['phase'] != last_phase:
                print(f"Phase: {analysis['phase']}", flush=True)
                last_phase = analysis['phase']
            if analysis['status'] in {'failed', 'cancelled'}:
                raise SystemExit(f"Analysis stopped: {analysis.get('error', analysis['status'])}")
            if analysis['status'] == 'ready':
                break
            time.sleep(2)
        else:
            raise SystemExit('Analysis did not finish within the configured test deadline.')
        assert analysis['graphReady'], 'Graph must be ready'
        graph_response = client.get(f'/analyses/{analysis_id}/graph')
        graph_response.raise_for_status()
        graph = graph_response.json()
        assert 0 < len(graph['nodes']) <= 100
        assert len(graph['edges']) <= 200
        ids = {node['id'] for node in graph['nodes']}
        assert all(edge['source'] in ids and edge['target'] in ids for edge in graph['edges'])
        search_response = client.get(f'/analyses/{analysis_id}/search', params={'q': 'rateLimit' if args.demo else 'index'})
        search_response.raise_for_status()
        results = search_response.json()['results']
        if args.demo:
            target = next(item for item in results if item.get('name') == 'rateLimit')
            file_id = 'file:' + target['path']
            source = client.get(f'/analyses/{analysis_id}/files/{quote(file_id, safe="")}')
            source.raise_for_status()
            assert 'MAX_REQUESTS = 30' in source.json()['content']
            deps = client.post(f'/analyses/{analysis_id}/chat', json={'message': 'What depends on this file?', 'selectedNodeId': file_id, 'action': 'dependents'})
            deps.raise_for_status()
            assert deps.json()['mode'] == 'graph'
        if args.rag:
            assert analysis['ragReady'], 'Atlas retrieval must be ready for --rag'
            chat = client.post(f'/analyses/{analysis_id}/chat', json={'message': 'Where is rate limiting implemented?' if args.demo else 'Explain the main entry point with source citations.'})
            if chat.status_code >= 400:
                raise SystemExit(f"AI request stopped: {chat.json().get('error', {}).get('message', 'service unavailable')}")
            chat.raise_for_status()
            answer = chat.json()
            assert answer['mode'] == 'rag'
            assert answer['citations'], 'The answer must cite source evidence'
            for citation in answer['citations']:
                assert citation['analysisId'] == analysis_id
                assert citation['commitSha'] == analysis['commitSha']
                source = client.get(f'/analyses/{analysis_id}/files/{quote(citation["fileId"], safe="")}')
                source.raise_for_status()
                assert 1 <= citation['startLine'] <= citation['endLine'] <= source.json()['lineCount']
            print(json.dumps({'citationsVerified': len(answer['citations'])}), flush=True)
        print(json.dumps({'result': 'passed', 'counts': analysis['counts'], 'ragReady': analysis['ragReady'], 'url': f'{args.base}/analysis/{analysis_id}'}), flush=True)


if __name__ == '__main__':
    main()
