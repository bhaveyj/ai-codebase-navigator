import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from navigator import auth
from navigator.analysis import select_preview_chunks
from navigator.api import main
from navigator.config import Settings
from navigator.contracts import DomainError
from navigator.demo import answer_id, routes_answer
from navigator.storage import AsyncStore, LocalStore


def test_github_login_uses_one_time_state_and_server_session(tmp_path, monkeypatch):
    store = LocalStore(tmp_path)
    settings = Settings(_env_file=None, GITHUB_OAUTH_CLIENT_ID="client",
                        GITHUB_OAUTH_CLIENT_SECRET="secret")
    start = auth.start_login(store, settings)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    assert "code_challenge" in start.headers["location"]

    class Github:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def post(self, url, **kwargs):
            assert kwargs["data"]["code_verifier"]
            return httpx.Response(200, json={"access_token": "temporary-token"}, request=httpx.Request("POST", url))
        def get(self, url, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer temporary-token"
            return httpx.Response(200, json={"id": 123, "login": "alice", "avatar_url": ""}, request=httpx.Request("GET", url))

    monkeypatch.setattr(auth.httpx, "Client", lambda **_: Github())
    guest_response = Response()
    guest = auth.ensure_guest(store, type("Request", (), {"cookies": {}})(), guest_response, settings)
    guest_token = guest_response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    store.put("user_analyses", {"id": f"{guest['id']}:preview", "userId": guest["id"],
                                "analysisId": "preview", "repositoryId": "repo"})
    store.put("chat_turns", {"id": "guest-turn", "userId": guest["id"],
                             "analysisId": "preview", "question": "Hello"})
    finished = auth.finish_login(store, settings, state, state, "code", guest_token)
    assert finished.headers["location"] == "/"
    assert store.one("users", {"id": "github:123"})["login"] == "alice"
    assert len(store.find("auth_sessions")) == 1
    assert store.one("user_analyses", {"id": "github:123:preview"})
    assert store.one("chat_turns", {"id": "migrated:github:123:guest-turn"})["question"] == "Hello"
    with pytest.raises(DomainError, match="expired"):
        auth.finish_login(store, settings, state, state, "code")


def test_preview_selection_is_bounded_and_spans_files():
    chunks = [{"id": str(i), "path": path, "embeddingText": "x" * 100}
              for i, path in enumerate(["src/routes/a.ts", "src/routes/a.ts",
                                        "src/services/b.ts", "src/services/b.ts",
                                        "README.md", "README.md"])]
    chosen = select_preview_chunks(chunks, 300)
    assert len(chosen) == 3
    assert len({item["path"] for item in chosen}) == 3
    assert chosen == select_preview_chunks(chunks, 300)


def test_sample_route_answer_uses_static_source_line(tmp_path):
    store = LocalStore(tmp_path)
    store.put("files", {"id": "file:src/server/routes/auth.ts", "analysisId": "sample",
                        "path": "src/server/routes/auth.ts", "lineCount": 30})
    analysis = {"id": "sample", "commitSha": "a" * 40, "overview": {"routes": [{
        "filePath": "src/server/routes/auth.ts", "method": "POST", "route": "/login",
        "startLine": 8, "nodeId": "login"}]}}
    answer = routes_answer(store, analysis)
    assert answer["blocks"][0]["text"] == "POST /login is declared in src/server/routes/auth.ts."
    assert answer["citations"][0]["startLine"] == 8
    assert answer["blocks"][0]["citationIds"] == ["D1"]


def test_free_repo_and_question_claims_are_atomic(tmp_path):
    store = LocalStore(tmp_path)
    store.put("users", {"id": "github:1", "login": "one", "questionsUsed": 0})
    assert store.claim_free_repository("github:1", "repo-a", "analysis-a")
    assert store.claim_free_repository("github:1", "repo-a", "analysis-a")
    assert not store.claim_free_repository("github:1", "repo-a", "analysis-b")
    assert not store.claim_free_repository("github:1", "repo-b", "analysis-c")
    assert [store.reserve_question("github:1", 2) for _ in range(3)] == [True, True, False]
    store.refund_question("github:1")
    assert store.reserve_question("github:1", 2)


@pytest.fixture
def secured_client(tmp_path, monkeypatch):
    store = LocalStore(tmp_path)
    config = Settings(_env_file=None, NAVIGATOR_MODE="production", MONGODB_URI="mongodb://unused",
                      FREE_QUESTIONS=2)
    monkeypatch.setattr(main, "make_store", lambda _: store)
    monkeypatch.setattr(main, "AsyncStore", lambda *_args: AsyncStore(config, local=store))
    monkeypatch.setattr(main, "create_indexes", lambda *_args: None)
    monkeypatch.setattr(main, "recover_jobs", lambda *_args: None)
    monkeypatch.setattr(main, "QuotaGate", lambda *_args: type("Gate", (), {"close": lambda self: None})())
    profile = config.preview_analysis_profile
    for n in (1, 2):
        store.put("repositories", {"id": f"repo-{n}", "owner": "examples", "name": f"repo{n}",
                                   "url": f"https://github.com/examples/repo{n}", "defaultBranch": "main",
                                   "createdAt": "2026-01-01T00:00:00+00:00"})
        store.put("analyses", {"id": f"analysis-{n}", "repositoryId": f"repo-{n}",
                               "repositoryName": f"examples/repo{n}", "commitSha": "a" * 40,
                               "profile": profile, "status": "ready", "phase": "ready",
                               "graphReady": True, "ragReady": True, "jobId": f"job-{n}",
                               "counts": {"files": 0, "symbols": 0, "edges": 0, "chunks": 0},
                               "languages": [], "createdAt": "2026-01-01T00:00:00+00:00",
                               "warnings": [], "preview": True})
        store.put("jobs", {"id": f"job-{n}", "analysisId": f"analysis-{n}",
                           "status": "ready", "phase": "ready", "completed": 1, "total": 1})
        store.put("users", {"id": f"github:{n}", "login": f"user{n}", "questionsUsed": 0})
        store.put("user_analyses", {"id": f"github:{n}:analysis-{n}", "userId": f"github:{n}",
                                    "analysisId": f"analysis-{n}", "repositoryId": f"repo-{n}",
                                    "createdAt": "2026-01-01T00:00:00+00:00"})
    app = main.create_app(config)
    with TestClient(app) as client:
        def sign_in(user_id):
            token = f"session-for-{user_id}"
            store.put("auth_sessions", {"id": auth.digest(token), "userId": user_id,
                                        "expiresAt": int(time.time()) + 3600})
            client.cookies.set(auth.SESSION_COOKIE, token)
        client.sign_in = sign_in
        client.store = store
        yield client


def test_analysis_id_and_job_routes_are_private(secured_client):
    client = secured_client
    assert client.get("/api/v1/repositories").json() == {"repositories": [], "analyses": []}
    assert client.get("/api/v1/analyses/analysis-1").status_code == 401
    client.sign_in("github:1")
    assert len(client.get("/api/v1/repositories").json()["analyses"]) == 1
    assert client.get("/api/v1/analyses/analysis-1").status_code == 200
    assert client.get("/api/v1/analyses/analysis-2").status_code == 404
    assert client.get("/api/v1/jobs/job-2").status_code == 404
    assert client.get("/api/v1/analyses/analysis-2/chat/history").status_code == 404


def test_repository_list_shows_latest_analysis_once(secured_client):
    client = secured_client
    client.sign_in("github:1")
    newer = {**client.store.one("analyses", {"id": "analysis-1"}), "id": "analysis-full",
             "createdAt": "2026-01-02T00:00:00+00:00", "preview": False}
    client.store.put("analyses", newer)
    client.store.put("user_analyses", {"id": "github:1:analysis-full", "userId": "github:1",
                                       "analysisId": "analysis-full", "repositoryId": "repo-1",
                                       "createdAt": newer["createdAt"]})
    listing = client.get("/api/v1/repositories").json()
    assert len(listing["repositories"]) == 1
    assert listing["repositories"][0]["latestAnalysisId"] == "analysis-full"
    assert len(listing["analyses"]) == 2


def test_signed_in_questions_are_not_capped_and_unlink_does_not_delete_shared_cache(secured_client, monkeypatch):
    client = secured_client
    client.sign_in("github:1")
    monkeypatch.setattr(main, "answer_question", lambda *_: {"mode": "rag", "blocks": [
        {"text": "Answered", "kind": "fact", "citationIds": []}], "citations": [],
        "relatedNodeIds": [], "warnings": []})
    headers = {"Origin": "http://localhost:8080"}
    path = "/api/v1/analyses/analysis-1/chat"
    assert client.post(path, json={"message": "One"}, headers=headers).status_code == 200
    assert client.post(path, json={"message": "Two"}, headers=headers).status_code == 200
    assert client.post(path, json={"message": "Three"}, headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me").json()["limitsApply"] is False
    assert client.get(path + "/history").json()["turns"][0]["question"] == "One"
    assert client.delete("/api/v1/repositories/repo-1", headers=headers).status_code == 204
    assert client.store.one("analyses", {"id": "analysis-1"}) is not None
    assert client.get("/api/v1/analyses/analysis-1").status_code == 404


def test_prepared_demo_answer_is_public_and_does_not_use_quota(secured_client):
    client = secured_client
    client.store.update("analyses", {"id": "analysis-1"}, {"demo": True})
    answer = {"mode": "rag", "blocks": [{"text": "Example answer", "kind": "fact", "citationIds": []}],
              "citations": [], "relatedNodeIds": [], "warnings": []}
    client.store.put("demo_answers", {"id": answer_id("analysis-1", "How is this project structured?"),
                                      "analysisId": "analysis-1", "answer": answer})
    response = client.post("/api/v1/analyses/analysis-1/chat", json={"message": "How is this project structured?"})
    assert response.status_code == 200 and response.json()["blocks"][0]["text"] == "Example answer"
    assert client.post("/api/v1/analyses/analysis-1/chat", json={"message": "A different question"}).status_code == 401


def test_signed_in_failed_preview_can_be_removed_without_account_limit(secured_client):
    client = secured_client
    client.sign_in("github:1")
    client.store.update("users", {"id": "github:1"},
                        {"freeRepositoryId": "repo-1", "freeAnalysisId": "analysis-1"})
    client.store.update("analyses", {"id": "analysis-1"},
                        {"status": "failed", "graphReady": False, "ragReady": False})
    client.store.update("jobs", {"id": "job-1"}, {"status": "failed"})
    headers = {"Origin": "http://localhost:8080"}
    assert client.delete("/api/v1/repositories/repo-1", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me").json()["freeRepositoryUsed"] is False
    assert client.get("/api/v1/auth/me").json()["limitsApply"] is False


def test_signed_in_legacy_preview_can_be_upgraded(secured_client, monkeypatch):
    client = secured_client
    client.sign_in("github:1")
    client.store.update("users", {"id": "github:1"},
                        {"freeRepositoryId": "repo-1", "freeAnalysisId": "analysis-1"})
    headers = {"Origin": "http://localhost:8080"}
    client.store.update("repositories", {"id": "repo-1"}, {"githubId": 1})
    monkeypatch.setattr(main, "launch", lambda *_: None)
    client.app.state.quota.submission = lambda *_: None
    upgraded = client.post("/api/v1/analyses/analysis-1/upgrade", headers=headers)
    assert upgraded.status_code == 202
    assert upgraded.json()["analysis"]["preview"] is False
    assert client.store.one("analyses", {"id": upgraded.json()["analysis"]["id"]})["profile"] == client.app.state.settings.analysis_profile
    assert len(client.get("/api/v1/repositories").json()["analyses"]) == 2


def test_guest_preview_limit_and_signed_in_full_submissions(secured_client, monkeypatch):
    client = secured_client
    monkeypatch.setattr(main, "github_metadata", lambda url, _: {
        "owner": "examples", "name": url.rsplit("/", 1)[-1], "url": url,
        "defaultBranch": "main", "githubId": 100 if url.endswith("first") else 101,
        "commitSha": "b" * 40})
    monkeypatch.setattr(main, "launch", lambda *_: None)
    client.app.state.quota.submission = lambda *_: None
    headers = {"Origin": "http://localhost:8080"}
    session = client.get("/api/v1/auth/me").json()
    assert not session["authenticated"] and session["questionsRemaining"] == 2
    first = client.post("/api/v1/repositories", json={"url": "https://github.com/examples/first"}, headers=headers)
    assert first.status_code == 202 and first.json()["analysis"]["preview"] is True
    assert client.get("/api/v1/auth/me").json()["freeRepositoryUsed"] is True
    assert client.post("/api/v1/repositories", json={"url": "https://github.com/examples/second"}, headers=headers).status_code == 403
    client.sign_in("github:1")
    second = client.post("/api/v1/repositories", json={"url": "https://github.com/examples/second"}, headers=headers)
    assert second.status_code == 202 and second.json()["analysis"]["preview"] is False
    third = client.post("/api/v1/repositories", json={"url": "https://github.com/examples/first"}, headers=headers)
    assert third.status_code == 202 and third.json()["analysis"]["preview"] is False
    assert client.get("/api/v1/auth/me").json()["freeRepositoryUsed"] is False


def test_guest_questions_are_capped_and_guest_repositories_are_private(secured_client, monkeypatch):
    client = secured_client
    client.get("/api/v1/auth/me")
    guest = auth.current_guest(client.store, type("Request", (), {"cookies": client.cookies})())
    client.store.put("user_analyses", {"id": f"{guest['id']}:analysis-1", "userId": guest["id"],
                                       "analysisId": "analysis-1", "repositoryId": "repo-1",
                                       "createdAt": "2026-01-01T00:00:00+00:00"})
    monkeypatch.setattr(main, "answer_question", lambda *_: {"mode": "rag", "blocks": [
        {"text": "Answered", "kind": "fact", "citationIds": []}], "citations": [],
        "relatedNodeIds": [], "warnings": []})
    path = "/api/v1/analyses/analysis-1/chat"
    headers = {"Origin": "http://localhost:8080"}
    assert [client.post(path, json={"message": "Question"}, headers=headers).status_code
            for _ in range(3)] == [200, 200, 403]
    assert client.get("/api/v1/auth/me").json()["questionsRemaining"] == 0
    client.cookies.clear()
    client.get("/api/v1/auth/me")
    assert client.get("/api/v1/repositories").json()["analyses"] == []
    assert client.get("/api/v1/analyses/analysis-1").status_code == 404


def test_active_preview_must_be_cancelled_before_removal(secured_client):
    client = secured_client
    client.sign_in("github:1")
    client.store.update("jobs", {"id": "job-1"}, {"status": "running"})
    response = client.delete("/api/v1/repositories/repo-1",
                             headers={"Origin": "http://localhost:8080"})
    assert response.status_code == 409
    assert client.store.one("user_analyses", {"id": "github:1:analysis-1"})
