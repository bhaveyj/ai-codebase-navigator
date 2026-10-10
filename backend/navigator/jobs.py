import hashlib
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from celery import Celery

from .analysis import build_chunks, overview, run_analyzer
from .config import get_settings
from .contracts import ChatRequest, DomainError
from .ingestion import demo_files, fetch_files
from .rag import answer_question, embed_chunks, wait_search
from .quota import QuotaGate
from .storage import SCOPED, create_indexes, make_store

logger = logging.getLogger(__name__)
settings = get_settings()
celery_app = Celery("navigator", broker=settings.redis_url)
celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True, broker_transport_options={"visibility_timeout": 7200}, task_time_limit=1800, task_soft_time_limit=1740, beat_schedule={"recover-jobs": {"task": "navigator.reconcile", "schedule": 10.0}})
local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="repository-analysis")
_local_futures = {}
_future_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def create_job(store, metadata, config, demo=False):
    repository_id = "demo-beacon-store" if demo else f"github-{metadata['githubId']}"
    analysis_id = hashlib.sha256(f"{repository_id}:{metadata['commitSha']}:{config.analysis_profile}".encode()).hexdigest()[:24]
    existing = store.one("analyses", {"id": analysis_id})
    repository = store.ensure("repositories", {"id": repository_id, **{key: metadata[key] for key in ("owner", "name", "url", "defaultBranch")}, **({"githubId": metadata["githubId"]} if not demo else {}), "createdAt": now()})
    if existing:
        return {"repository": repository, "analysis": existing, "job": store.one("jobs", {"id": existing["jobId"]})}, False
    job_id = f"job-{analysis_id}"
    analysis = store.ensure("analyses", {"id": analysis_id, "repositoryId": repository_id, "repositoryName": f"{metadata['owner']}/{metadata['name']}", "commitSha": metadata["commitSha"], "profile": config.analysis_profile, "status": "queued", "phase": "queued", "graphReady": False, "ragReady": False, "jobId": job_id, "counts": {"files": 0, "symbols": 0, "edges": 0, "chunks": 0}, "languages": [], "createdAt": now(), "warnings": [], "demo": demo})
    job = store.ensure("jobs", {"id": job_id, "analysisId": analysis_id, "status": "queued", "phase": "queued", "completed": 0, "total": 0, "message": "Waiting for analysis worker", "checkpoints": [], "attempts": 0, "cancelRequested": False, "createdAt": now(), "heartbeatAt": now()})
    return {"repository": repository, "analysis": analysis, "job": job}, True


def launch(job_id, config):
    if config.mode == "local":
        with _future_lock:
            previous = _local_futures.get(job_id)
            if previous and not previous.done():
                return
            _local_futures[job_id] = local_executor.submit(run_job, job_id, config)
    else:
        analyze_repository.apply_async(args=[job_id], task_id=f"analysis-{job_id}")


class Cancelled(Exception):
    pass


class LeaseLost(Exception):
    pass


def cancellation_checker(store, job_id, token, interval=0.5):
    """Bound database reads during tight chunk loops while checking promptly."""
    last_checked = float("-inf")

    def check():
        nonlocal last_checked
        current_time = time.monotonic()
        if current_time - last_checked < interval:
            return
        current = store.one("jobs", {"id": job_id})
        if not current or current.get("cancelRequested"):
            raise Cancelled()
        if current.get("leaseToken") != token:
            raise LeaseLost()
        last_checked = current_time

    return check


def run_job(job_id, config):
    store = make_store(config)
    job = store.one("jobs", {"id": job_id})
    # Only explicit retries/reconciliation can queue work. Stale broker deliveries
    # must not restart a failed job after its daily quota has been exhausted.
    if not job or job["status"] != "queued" or job.get("cancelRequested"):
        store.close()
        return
    if (job.get("nextAttemptAt") or "") > now():
        store.close()
        return
    token = uuid.uuid4().hex
    claimed = store.update("jobs", {"id": job_id, "status": job["status"], "heartbeatAt": job["heartbeatAt"]}, {"status": "running", "leaseToken": token, "heartbeatAt": now(), "attempts": job.get("attempts", 0) + 1, "error": None, "nextAttemptAt": None})
    if not claimed:
        store.close()
        return
    analysis_id = job["analysisId"]
    checkpoints = set(job.get("checkpoints", []))
    stop_heartbeat = threading.Event()
    quota = None
    check_cancel = cancellation_checker(store, job_id, token)

    def heartbeat():
        while not stop_heartbeat.wait(15):
            try:
                store.update("jobs", {"id": job_id, "leaseToken": token}, {"heartbeatAt": now()})
            except Exception:
                logger.warning("Job heartbeat unavailable for %s", job_id)

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    def progress(phase, completed=0, total=0, message=None):
        check_cancel()
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"phase": phase, "completed": completed, "total": total, "message": message or phase.replace("_", " ").capitalize(), "heartbeatAt": now(), "nextAttemptAt": None})
        store.update("analyses", {"id": analysis_id}, {"status": "running", "phase": phase, "error": None})

    def embedding_backoff(delay, completed, total):
        progress("embedding_backoff", completed, total, "Gemini rate limit reached. Saved embeddings are retained; indexing resumes automatically.")
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"nextAttemptAt": retry_at})

    def checkpoint(name):
        check_cancel()
        checkpoints.add(name)
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"checkpoints": sorted(checkpoints), "heartbeatAt": now()})

    try:
        analysis = store.one("analyses", {"id": analysis_id})
        repo = store.one("repositories", {"id": analysis["repositoryId"]})
        if "fetching" not in checkpoints:
            progress("fetching", message="Fetching the pinned source snapshot")
            files, warnings = demo_files(config.demo_dir, analysis_id, config) if analysis.get("demo") else fetch_files(repo, analysis["commitSha"], analysis_id, config, check_cancel)
            if not any(file["language"] in {"javascript", "typescript"} for file in files):
                raise DomainError("NO_SUPPORTED_SOURCE", "No eligible JavaScript or TypeScript source files were found. Additional languages are planned.")
            check_cancel()
            store.put_many("files", files)
            warnings += [f"Secret-like content redacted: {file['path']}" for file in files if file.get("redacted")]
            store.update("analyses", {"id": analysis_id}, {"warnings": warnings, "languages": sorted({file["language"] for file in files if file["language"] in {"javascript", "typescript"}})})
            checkpoint("fetching")
        files = store.find("files", {"analysisId": analysis_id})
        if "graph" not in checkpoints:
            progress("parsing", 0, len(files), "Extracting declarations and resolving static relationships")
            records = run_analyzer(files, analysis_id, config, check_cancel)
            progress("building_graph", len(files), len(files), "Validating and storing the dependency graph")
            for collection, documents in records.items():
                check_cancel()
                store.put_many(collection, documents)
            warnings = store.one("analyses", {"id": analysis_id})["warnings"]
            warnings += [diagnostic["message"] for diagnostic in records["diagnostics"][:100]]
            if records["unresolved_references"]:
                warnings.append(f"{len(records['unresolved_references'])} unresolved static references; no edges were invented for them.")
            store.update("analyses", {"id": analysis_id}, {"graphReady": True, "warnings": list(dict.fromkeys(warnings)), "counts": {"files": len(files), "symbols": sum(node["kind"] == "symbol" for node in records["nodes"]), "edges": len(records["edges"]), "chunks": 0}, "overview": overview(files, records["nodes"])})
            checkpoint("graph")
        if "chunks" not in checkpoints:
            progress("creating_chunks", message="Building source chunks around declarations")
            regions = store.find("regions", {"analysisId": analysis_id})
            nodes = store.find("nodes", {"analysisId": analysis_id})
            chunks = build_chunks(files, regions, nodes, config)
            for chunk in chunks:
                chunk["repositoryId"] = analysis["repositoryId"]
            store.put_many("chunks", chunks)
            counts = store.one("analyses", {"id": analysis_id})["counts"]
            counts["chunks"] = len(chunks)
            store.update("analyses", {"id": analysis_id}, {"counts": counts})
            checkpoint("chunks")
        if store.is_mongo and config.gemini_api_key:
            quota = QuotaGate(config)
            if "embeddings" not in checkpoints:
                progress("creating_embeddings", message="Creating Gemini embeddings")
                chunks = store.find("chunks", {"analysisId": analysis_id})
                embed_chunks(store, chunks, config, lambda done, total: progress("creating_embeddings", done, total), check_cancel, on_backoff=embedding_backoff, quota=quota)
                checkpoint("embeddings")
            if "search" not in checkpoints:
                progress("preparing_search", message="Waiting for Atlas vector and lexical indexes")
                create_indexes(store, config, search=True)
                chunks = store.find("chunks", {"analysisId": analysis_id})
                wait_search(store, analysis_id, chunks, config, check_cancel)
                store.update("analyses", {"id": analysis_id}, {"ragReady": True})
                checkpoint("search")
            if "summary" not in checkpoints:
                progress("summarizing", message="Generating a source-grounded architecture summary")
                try:
                    current = store.one("analyses", {"id": analysis_id})
                    summary = answer_question(store, current, ChatRequest(message="Summarize the repository architecture, entry points, major modules and integrations, with source evidence."), config, quota=quota)
                    overview_data = current.get("overview", {})
                    overview_data["summary"] = "\n\n".join(block["text"] for block in summary["blocks"])
                    overview_data["citations"] = summary["citations"]
                    overview_data["blocks"] = summary["blocks"]
                    store.update("analyses", {"id": analysis_id}, {"overview": overview_data})
                except DomainError as error:
                    if error.code in {"GEMINI_CAPACITY_WAIT", "GEMINI_RATE_LIMITED", "GEMINI_QUOTA_UNKNOWN", "GEMINI_QUOTA_EXHAUSTED"}:
                        raise
                    current = store.one("analyses", {"id": analysis_id})
                    store.update("analyses", {"id": analysis_id}, {"warnings": current["warnings"] + ["Architecture summary could not be generated. Source, graph and retrieval remain available."]})
                checkpoint("summary")
        else:
            current = store.one("analyses", {"id": analysis_id})
            note = "Graph and source analysis complete. AI answers require MongoDB Atlas and GEMINI_API_KEY in production mode." if not store.is_mongo else "Graph and source analysis complete. Configure GEMINI_API_KEY and retry to create embeddings."
            store.update("analyses", {"id": analysis_id}, {"warnings": list(dict.fromkeys(current["warnings"] + [note]))})
        check_cancel()
        store.update("analyses", {"id": analysis_id}, {"status": "ready", "phase": "ready", "error": None})
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"status": "ready", "phase": "ready", "completed": 1, "total": 1, "message": "Analysis ready", "error": None, "heartbeatAt": now()})
        store.update("repositories", {"id": analysis["repositoryId"]}, {"latestAnalysisId": analysis_id})
    except Cancelled:
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"status": "cancelled", "phase": "cancelled", "message": "Analysis cancelled", "heartbeatAt": now()})
        store.update("analyses", {"id": analysis_id}, {"status": "cancelled", "phase": "cancelled"})
    except LeaseLost:
        logger.info("Stopped stale worker for %s", job_id)
    except DomainError as error:
        if error.code in {"INDEXING_SLICE_COMPLETE", "GEMINI_CAPACITY_WAIT", "GEMINI_RATE_LIMITED", "GEMINI_QUOTA_UNKNOWN", "GEMINI_QUOTA_EXHAUSTED"}:
            delay = max(1, error.retry_after_seconds or 180)
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            store.update("jobs", {"id": job_id, "leaseToken": token}, {"status": "queued", "phase": "waiting_for_capacity", "message": error.message, "error": None, "nextAttemptAt": retry_at, "heartbeatAt": now()})
            store.update("analyses", {"id": analysis_id}, {"status": "queued", "phase": "waiting_for_capacity", "error": None})
            logger.info("Analysis %s waiting for Gemini capacity until %s", analysis_id, retry_at)
            return None
        logger.warning("Analysis %s failed: %s", analysis_id, error.code)
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"status": "failed", "message": error.message, "error": error.message, "heartbeatAt": now()})
        store.update("analyses", {"id": analysis_id}, {"status": "failed", "error": error.message})
        return error
    except Exception as error:
        safe_message = "Analysis failed because a service or runtime is unavailable. Check server logs and retry."
        logger.error("Analysis %s failed: %s", analysis_id, type(error).__name__)
        store.update("jobs", {"id": job_id, "leaseToken": token}, {"status": "failed", "message": safe_message, "error": safe_message, "heartbeatAt": now()})
        store.update("analyses", {"id": analysis_id}, {"status": "failed", "error": safe_message})
        return error
    finally:
        if quota:
            quota.close()
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1)
        store.close()


@celery_app.task(bind=True, name="navigator.analyze", max_retries=3)
def analyze_repository(self, job_id):
    config = get_settings()
    error = run_job(job_id, config)
    if isinstance(error, DomainError) and error.retryable and error.status >= 500 and self.request.retries < 3:
        store = make_store(config)
        job = store.one("jobs", {"id": job_id})
        if job and not job.get("cancelRequested"):
            delay = max(min(240, (60 if error.code == "GEMINI_RATE_LIMITED" else 10) * 2 ** self.request.retries), error.retry_after_seconds or 0)
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            store.update("jobs", {"id": job_id, "status": "failed"}, {"status": "queued", "message": "Retrying a transient service failure", "error": None, "nextAttemptAt": next_attempt})
            store.update("analyses", {"id": job["analysisId"]}, {"status": "queued", "error": None})
            store.close()
            raise self.retry(countdown=delay)
        store.close()


def recover_jobs(config, startup=False):
    store = make_store(config)
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    for job in store.find("jobs", {"status": {"$in": ["queued", "running"]}}):
        if job.get("nextAttemptAt", "") and job["nextAttemptAt"] > now():
            continue
        if job.get("cancelRequested"):
            # A live worker must acknowledge cancellation before deletion is safe.
            if job["status"] == "running" and job.get("heartbeatAt", "") >= threshold:
                continue
            store.update("jobs", {"id": job["id"]}, {"status": "cancelled", "phase": "cancelled"})
            store.update("analyses", {"id": job["analysisId"]}, {"status": "cancelled", "phase": "cancelled"})
            continue
        if job["status"] == "running":
            if not (startup and config.mode == "local") and job.get("heartbeatAt", "") >= threshold:
                continue
            changed = store.update("jobs", {"id": job["id"], "heartbeatAt": job["heartbeatAt"]}, {"status": "queued", "leaseToken": None, "message": "Recovering from an interrupted worker"})
            if not changed:
                continue
        launch(job["id"], config)
    store.close()


@celery_app.task(name="navigator.reconcile")
def reconcile():
    recover_jobs(get_settings())


def delete_repository(store, repository_id):
    analyses = store.find("analyses", {"repositoryId": repository_id})
    for analysis in analyses:
        store.update("jobs", {"id": analysis["jobId"]}, {"cancelRequested": True})
        for collection in SCOPED:
            store.delete(collection, {"analysisId": analysis["id"]})
        store.delete("jobs", {"id": analysis["jobId"]})
        store.delete("analyses", {"id": analysis["id"]})
    store.delete("repositories", {"id": repository_id})
