from datetime import datetime, timedelta, timezone

import pytest

from navigator.config import Settings
from navigator.jobs import Cancelled, LeaseLost, cancellation_checker, create_job, now, recover_jobs, run_job
from navigator.storage import LocalStore


def test_cancellation_checker_bounds_reads_and_detects_changes(monkeypatch):
    from navigator import jobs

    clock = [0.0]
    document = {"leaseToken": "current", "cancelRequested": False}
    calls = []

    class Store:
        def one(self, collection, query):
            calls.append((collection, query))
            return document.copy()

    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
    check = cancellation_checker(Store(), "job-1", "current")
    check()
    clock[0] = 0.25
    check()
    assert len(calls) == 1
    clock[0] = 0.5
    check()
    assert len(calls) == 2
    document["cancelRequested"] = True
    clock[0] = 1.0
    with pytest.raises(Cancelled):
        check()
    document["cancelRequested"] = False
    document["leaseToken"] = "replacement"
    clock[0] = 1.5
    with pytest.raises(LeaseLost):
        check()


def test_live_worker_must_acknowledge_cancel_before_cleanup(tmp_path, monkeypatch):
    from navigator import jobs
    store = LocalStore(tmp_path)
    config = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_DATA_DIR=tmp_path)
    payload, _ = create_job(store, {"owner": "example", "name": "fixture", "url": "local://fixture", "defaultBranch": "main", "commitSha": "abc"}, config, True)
    id = payload["job"]["id"]
    store.update("jobs", {"id": id}, {"status": "running", "cancelRequested": True, "heartbeatAt": now()})
    monkeypatch.setattr(jobs, "launch", lambda *_: None)
    recover_jobs(config)
    assert store.one("jobs", {"id": id})["status"] == "running"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    store.update("jobs", {"id": id}, {"heartbeatAt": expired})
    recover_jobs(config)
    assert store.one("jobs", {"id": id})["status"] == "cancelled"


def test_stale_worker_requeued_with_checkpoints_and_old_lease_revoked(tmp_path, monkeypatch):
    from navigator import jobs
    store = LocalStore(tmp_path)
    config = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_DATA_DIR=tmp_path)
    payload, _ = create_job(store, {"owner": "example", "name": "fixture", "url": "local://fixture", "defaultBranch": "main", "commitSha": "abc"}, config, True)
    id = payload["job"]["id"]
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    store.update("jobs", {"id": id}, {"status": "running", "leaseToken": "old", "checkpoints": ["fetching", "graph"], "heartbeatAt": expired})
    launched = []
    monkeypatch.setattr(jobs, "launch", lambda job_id, _: launched.append(job_id))
    recover_jobs(config)
    recovered = store.one("jobs", {"id": id})
    assert recovered["status"] == "queued" and recovered["leaseToken"] is None
    assert recovered["checkpoints"] == ["fetching", "graph"]
    assert launched == [id]


def test_stale_delivery_does_not_restart_failed_job(tmp_path):
    config = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_DATA_DIR=tmp_path)
    store = LocalStore(tmp_path)
    payload, _ = create_job(store, {"owner": "example", "name": "fixture", "url": "local://fixture", "defaultBranch": "main", "commitSha": "abc"}, config, True)
    id = payload["job"]["id"]
    store.update("jobs", {"id": id}, {"status": "failed", "error": "Daily quota exhausted", "completed": 704})
    assert run_job(id, config) is None
    job = store.one("jobs", {"id": id})
    assert job["status"] == "failed" and job["completed"] == 704 and job["attempts"] == 0


def test_scheduler_waits_for_quota_deadline_before_launching(tmp_path, monkeypatch):
    from navigator import jobs
    config = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_DATA_DIR=tmp_path)
    store = LocalStore(tmp_path)
    payload, _ = create_job(store, {"owner": "example", "name": "fixture", "url": "local://fixture", "defaultBranch": "main", "commitSha": "abc"}, config, True)
    id = payload["job"]["id"]
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.update("jobs", {"id": id}, {"nextAttemptAt": future, "checkpoints": ["fetching", "graph", "chunks"], "completed": 704})
    launched = []
    monkeypatch.setattr(jobs, "launch", lambda job_id, _: launched.append(job_id))
    recover_jobs(config)
    assert not launched
    assert run_job(id, config) is None
    assert store.one("jobs", {"id": id})["completed"] == 704
    store.update("jobs", {"id": id}, {"nextAttemptAt": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    recover_jobs(config)
    assert launched == [id]
