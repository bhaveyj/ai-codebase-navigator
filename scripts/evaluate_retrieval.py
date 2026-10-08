"""Evaluate the real Atlas/Gemini retrieval pipeline on the Beacon fixture.

This makes billable embedding requests. Run after indexing the demo in production:
python scripts/evaluate_retrieval.py ANALYSIS_ID [--output report.json]
No mocked retrieval, vectors, or passing scores are produced without services.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    from dotenv import load_dotenv
    from navigator.config import Settings
    from navigator.contracts import ChatRequest
    from navigator.rag import retrieve
    from navigator.storage import MongoStore
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('analysis_id')
    parser.add_argument('--output', type=Path, default=ROOT / 'retrieval-report.json')
    args = parser.parse_args()
    settings = Settings(NAVIGATOR_MODE='production')
    if not settings.mongodb_uri or not settings.gemini_api_key:
        raise SystemExit('Configure real MONGODB_URI and GEMINI_API_KEY before running this evaluation.')
    store = MongoStore(settings)
    try:
        analysis = store.one('analyses', {'id': args.analysis_id})
        if not analysis or not analysis.get('ragReady'):
            raise SystemExit('Analysis must exist and have completed real Atlas/Gemini indexing.')
        if analysis.get('repositoryName') != 'examples/beacon-store':
            raise SystemExit('This evaluation targets the bundled Beacon Store fixture.')
        dataset = json.loads((ROOT / 'tests/evaluations/beacon-questions.json').read_text('utf-8'))
        results = []
        for case in dataset['cases']:
            evidence, _, _ = retrieve(store, analysis, ChatRequest(message=case['question']), settings)
            paths = list(dict.fromkeys(chunk['path'] for chunk in evidence[:10]))
            found = set(case['expectedPaths']).intersection(paths)
            recall = len(found) / len(case['expectedPaths'])
            results.append({'id': case['id'], 'recallAt10': recall, 'retrievedPaths': paths})
            print(f"{case['id']}: recall@10={recall:.2f}", flush=True)
        score = sum(result['recallAt10'] for result in results) / len(results)
        report = {'analysisId': analysis['id'], 'commitSha': analysis['commitSha'], 'recallAt10': score,
                  'target': 0.85, 'passed': score >= 0.85, 'results': results,
                  'note': 'Absence cases and unsupported relationship claims require separate answer review.'}
        args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'Recall@10: {score:.1%}; report: {args.output}')
        raise SystemExit(0 if report['passed'] else 1)
    finally:
        store.close()


if __name__ == '__main__':
    main()
