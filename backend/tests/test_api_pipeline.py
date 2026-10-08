import os
import shutil
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from navigator.api import main
from navigator.config import Settings
from navigator.jobs import run_job


@pytest.fixture
def client(tmp_path, monkeypatch):
    binary = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not binary:
        pytest.skip("Real analyzer integration requires Node and installed analyzer dependencies")
    config = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_DATA_DIR=tmp_path,
                      NODE_BINARY=binary, GEMINI_API_KEY="", MONGODB_URI="")
    monkeypatch.setattr(main, "launch", lambda job_id, settings: run_job(job_id, settings))
    app = main.create_app(config)
    with TestClient(app) as test:
        submission = test.post("/api/v1/demo")
        assert submission.status_code == 202
        test.analysis_id = submission.json()["analysis"]["id"]
        test.job_id = submission.json()["job"]["id"]
        yield test


def test_real_fixture_graph_source_and_search(client):
    analysis = client.get(f"/api/v1/analyses/{client.analysis_id}").json()
    assert analysis["status"] == "ready" and analysis["graphReady"] and not analysis["ragReady"]
    assert analysis["counts"]["files"] == 20
    assert analysis["counts"]["symbols"] >= 60
    graph = client.get(f"/api/v1/analyses/{client.analysis_id}/graph").json()
    assert 0 < len(graph["nodes"]) < 30
    assert all("analysisId" not in n for n in graph["nodes"])
    source = client.get(f"/api/v1/analyses/{client.analysis_id}/files/{quote('file:src/server/middleware/rate-limit.ts', safe='')}")
    assert source.status_code == 200
    assert "MAX_REQUESTS = 30" in source.json()["content"]
    assert "contentHash" not in source.json()
    search = client.get(f"/api/v1/analyses/{client.analysis_id}/search", params={"q": "rateLimit"}).json()
    assert any(result["name"] == "rateLimit" for result in search["results"])


def test_citations_resolve_to_same_analysis(client):
    answer = client.post(f"/api/v1/analyses/{client.analysis_id}/chat", json={"message": "What depends on this file?", "selectedNodeId": "file:src/server/middleware/rate-limit.ts", "action": "dependents"})
    assert answer.status_code == 200
    data = answer.json()
    assert data["mode"] == "graph" and data["citations"]
    for citation in data["citations"]:
        assert citation["analysisId"] == client.analysis_id
        file = client.get(f"/api/v1/analyses/{client.analysis_id}/files/{quote(citation['fileId'], safe='')}").json()
        assert 1 <= citation["startLine"] <= citation["endLine"] <= file["lineCount"]
    foreign = client.post(f"/api/v1/analyses/{client.analysis_id}/chat", json={"action": "dependencies", "selectedNodeId": "file:foreign.ts"})
    assert foreign.status_code == 404


def test_file_boundary_and_internal_job_fields(client):
    client.app.state.store.put("files", {"id": "file:foreign.ts", "analysisId": "foreign", "path": "foreign.ts", "content": "secret", "lineCount": 1, "language": "typescript"})
    assert client.get(f"/api/v1/analyses/{client.analysis_id}/files/file:foreign.ts").status_code == 404
    job = client.get(f"/api/v1/jobs/{client.job_id}").json()
    assert "leaseToken" not in job and "checkpoints" not in job
    event = client.get(f"/api/v1/jobs/{client.job_id}/events")
    assert "event: progress" in event.text and "leaseToken" not in event.text


def test_missing_ai_has_explicit_error_without_fictional_answer(client):
    response = client.post(f"/api/v1/analyses/{client.analysis_id}/chat", json={"message": "How does authentication work?"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ATLAS_UNAVAILABLE"
    assert "blocks" not in response.json()


def test_retry_can_wait_for_quota_reset_without_losing_progress(client, monkeypatch):
    client.app.state.store.update("jobs", {"id": client.job_id}, {"status": "failed", "completed": 7, "total": 10})
    client.app.state.store.update("analyses", {"id": client.analysis_id}, {"status": "failed"})
    launched = []
    monkeypatch.setattr(main, "launch", lambda *_: launched.append(True))
    retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = client.post(f"/api/v1/jobs/{client.job_id}/retry", json={"retryAt": retry_at})
    assert result.status_code == 200
    job = result.json()
    assert job["status"] == "queued" and job["phase"] == "waiting_for_quota"
    assert job["nextAttemptAt"] == retry_at and job["completed"] == 7
    assert not launched
    analysis = client.get(f"/api/v1/analyses/{client.analysis_id}").json()
    assert analysis["graphReady"] and not analysis["ragReady"]
    assert analysis["phase"] == "waiting_for_quota"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert client.post(f"/api/v1/jobs/{client.job_id}/retry", json={"retryAt": past}).status_code == 400
    assert client.post(f"/api/v1/jobs/{client.job_id}/retry", json={"retryAt": "2026-10-09T07:05:00"}).status_code == 422


def test_request_bounds_and_cross_origin_mutations(client):
    denied = client.post("/api/v1/demo", headers={"Origin": "https://untrusted.example"})
    assert denied.status_code == 403
    large = client.post("/api/v1/repositories", content=b"x" * 66000, headers={"Content-Type": "application/json"})
    assert large.status_code == 413
    bad_path = client.get(f"/api/v1/analyses/{client.analysis_id}/tree", params={"parent": "../secret"})
    assert bad_path.status_code == 400


def test_submission_reuses_commit_snapshot_and_deletion_cleans_artifacts(client):
    again = client.post("/api/v1/demo")
    assert again.status_code == 200
    assert again.json()["analysis"]["id"] == client.analysis_id
    assert len(client.app.state.store.find("analyses")) == 1
    deleted = client.delete("/api/v1/repositories/demo-beacon-store")
    assert deleted.status_code == 204
    assert not client.app.state.store.find("chunks", {"analysisId": client.analysis_id})
    assert client.get(f"/api/v1/analyses/{client.analysis_id}").status_code == 404
