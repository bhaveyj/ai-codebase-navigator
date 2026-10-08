from types import SimpleNamespace

import pytest

from navigator import rag
from navigator.config import Settings
from navigator.contracts import DomainError
from navigator.jobs import Cancelled
from navigator.storage import LocalStore


def chunks(count):
    return [{"id": f"c{n}", "analysisId": "a", "inputHash": f"h{n}", "embeddingText": str(n)} for n in range(count)]


def test_rate_limit_retries_only_pending_batch_and_reports_cooldown(monkeypatch, tmp_path):
    store = LocalStore(tmp_path)
    config = Settings(_env_file=None, embedding_batch_size=1, embedding_inputs_per_minute=0)
    calls, delays, pauses = [], [], []

    def embed(texts, _):
        calls.append(texts)
        if len(calls) == 2:
            raise DomainError("GEMINI_RATE_LIMITED", "rate", 503, retry_after_seconds=77)
        return [[1.0, 0.0]]

    monkeypatch.setattr(rag, "embed_batch", embed)
    monkeypatch.setattr(rag, "wait_for_embedding_delay", lambda delay, cancel: delays.append(delay))
    rag.embed_chunks(store, chunks(2), config, lambda *_: None, lambda: None, lambda *args: pauses.append(args))
    assert calls == [["0"], ["1"], ["1"]]
    assert pauses == [(77, 1, 2)]
    assert 77 in delays
    assert len(store.find("chunks")) == 2
    assert len(store.find("embedding_cache")) == 2


def test_batch_pacing_counts_source_inputs_not_http_batches(monkeypatch, tmp_path):
    store = LocalStore(tmp_path)
    config = Settings(_env_file=None, embedding_batch_size=2, embedding_inputs_per_minute=80)
    delays = []
    monkeypatch.setattr(rag.time, "monotonic", lambda: 10)
    monkeypatch.setattr(rag, "wait_for_embedding_delay", lambda delay, _: delays.append(delay))
    monkeypatch.setattr(rag, "embed_batch", lambda texts, _: [[1.0] for _ in texts])
    rag.embed_chunks(store, chunks(4), config, lambda *_: None, lambda: None)
    assert delays == [0, 1.5]


def test_daily_quota_is_not_retried(monkeypatch, tmp_path):
    store = LocalStore(tmp_path)
    calls = []

    def embed(*_):
        calls.append(1)
        raise DomainError("GEMINI_QUOTA_EXHAUSTED", "daily quota", 503, retryable=False)

    monkeypatch.setattr(rag, "embed_batch", embed)
    with pytest.raises(DomainError):
        rag.embed_chunks(store, chunks(1), Settings(_env_file=None), lambda *_: None, lambda: None)
    assert len(calls) == 1


def test_backoff_can_be_cancelled_without_waiting_for_entire_delay(monkeypatch):
    elapsed = [0]
    monkeypatch.setattr(rag.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def cancel():
        if elapsed[0] >= 2:
            raise Cancelled()

    with pytest.raises(Cancelled):
        rag.wait_for_embedding_delay(120, cancel)
    assert elapsed[0] == 2


def test_provider_retry_info_is_retained_without_exposing_provider_body():
    error = SimpleNamespace(code=429, details={"error": {"message": "secret project/key details", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "28.5s"},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "EmbedRequestsPerMinute", "quotaValue": "100"}]},
    ]}})
    result = rag.provider_error(error, "embedding")
    assert result.code == "GEMINI_RATE_LIMITED" and result.retryable
    assert result.retry_after_seconds == 28.5
    assert "secret" not in result.message


@pytest.mark.parametrize("quota_id, value", [("EmbedRequestsPerDay", "1000"), ("EmbedRequestsPerMinute", "0")])
def test_provider_exhausted_quota_does_not_schedule_automatic_retries(quota_id, value):
    error = SimpleNamespace(code=429, details={"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id, "quotaValue": value}]},
    ]}})
    result = rag.provider_error(error, "embedding")
    assert result.code == "GEMINI_QUOTA_EXHAUSTED" and not result.retryable


def test_worker_remains_running_and_publishes_retry_deadline_during_cooldown(monkeypatch, tmp_path):
    from navigator import jobs

    config = Settings(_env_file=None, GEMINI_API_KEY="test", embedding_inputs_per_minute=0)
    store = LocalStore(tmp_path)
    store.is_mongo = True
    payload, _ = jobs.create_job(store, {"owner": "example", "name": "fixture", "url": "local://fixture", "defaultBranch": "main", "commitSha": "abc"}, config, True)
    job_id, analysis_id = payload["job"]["id"], payload["analysis"]["id"]
    store.update("jobs", {"id": job_id}, {"checkpoints": ["fetching", "graph", "chunks"]})
    store.update("analyses", {"id": analysis_id}, {"graphReady": True})
    store.put("chunks", {**chunks(1)[0], "analysisId": analysis_id})
    monkeypatch.setattr(jobs, "make_store", lambda _: store)
    monkeypatch.setattr(jobs, "create_indexes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "wait_search", lambda *_: None)
    monkeypatch.setattr(jobs, "answer_question", lambda *_: {"blocks": [{"text": "Fixture summary"}], "citations": []})
    calls, pauses = [], []

    def embed(*_):
        calls.append(1)
        if len(calls) == 1:
            raise DomainError("GEMINI_RATE_LIMITED", "rate", 503)
        return [[1.0]]

    def wait(delay, cancel):
        cancel()
        if delay > 0:
            job = store.one("jobs", {"id": job_id})
            analysis = store.one("analyses", {"id": analysis_id})
            assert job["status"] == analysis["status"] == "running"
            assert job["phase"] == analysis["phase"] == "embedding_backoff"
            assert job["nextAttemptAt"] > jobs.now()
            assert analysis["graphReady"] and not analysis["ragReady"]
            pauses.append(job["nextAttemptAt"])

    monkeypatch.setattr(rag, "embed_batch", embed)
    monkeypatch.setattr(rag, "wait_for_embedding_delay", wait)
    assert jobs.run_job(job_id, config) is None
    assert len(pauses) == 1
    assert store.one("analyses", {"id": analysis_id})["ragReady"]
    assert store.one("jobs", {"id": job_id})["nextAttemptAt"] is None
