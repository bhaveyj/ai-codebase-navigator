import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import Settings, get_settings
from ..contracts import ChatRequest, DomainError, JobRetryRequest, SubmitRequest
from ..graph import project_graph
from ..ingestion import demo_files, github_metadata
from ..jobs import create_job, delete_repository, launch, now, recover_jobs
from ..rag import answer_question, dependency_answer
from ..storage import AsyncStore, create_indexes, make_store
from ..responses import Analysis, Capabilities, ChatResponse, GraphData, Job, RepositoryList, SearchResponse, SourceFile, Submission, TreeResponse

logger = logging.getLogger(__name__)


def create_app(config: Settings | None = None):
    config = config or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = make_store(config)
        app.state.async_store = None
        try:
            app.state.async_store = AsyncStore(config, app.state.store if config.mode == "local" else None)
            if config.mode == "production":
                await asyncio.to_thread(create_indexes, app.state.store, config, False)
            app.state.admission = asyncio.Lock()
            app.state.chat_slots = asyncio.Semaphore(2)
            try:
                await asyncio.to_thread(recover_jobs, config, True)
            except Exception as error:
                logger.warning("Job recovery unavailable: %s", type(error).__name__)
            yield
        finally:
            if app.state.async_store is not None:
                await app.state.async_store.close()
            app.state.store.close()

    app = FastAPI(title="AI Codebase Navigator", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts.split(","))
    app.add_middleware(CORSMiddleware, allow_origins=config.allowed_origins.split(","), allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"], allow_credentials=False)
    rates = defaultdict(deque)

    @app.middleware("http")
    async def request_bounds(request: Request, call_next):
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin not in config.allowed_origins.split(","):
                return error_response("ORIGIN_REJECTED", "This request origin is not allowed.", 403)
            size = request.headers.get("content-length", "0")
            if not size.isdigit() or int(size) > 64 * 1024:
                return error_response("REQUEST_TOO_LARGE", "Request body exceeds 64 KiB.", 413)
            # Bound chunked requests before allocating the complete body.
            parts, received = [], 0
            async for part in request.stream():
                received += len(part)
                if received > 64 * 1024:
                    return error_response("REQUEST_TOO_LARGE", "Request body exceeds 64 KiB.", 413)
                parts.append(part)
            request._body = b"".join(parts)
            key = (request.client.host if request.client else "local", request.url.path)
            attempts = rates[key]
            current = time.monotonic()
            while attempts and attempts[0] < current - 60:
                attempts.popleft()
            limit = 30 if request.url.path.endswith("/chat") else 12
            if len(attempts) >= limit:
                return error_response("RATE_LIMITED", "Too many requests. Please retry in a minute.", 429)
            attempts.append(current)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request, error):
        return error_response(error.code, error.message, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        return error_response("INVALID_REQUEST", "Request validation failed. Check the submitted values.", 422)

    @app.exception_handler(Exception)
    async def unknown_error(_request, error):
        logger.error("Request failed: %s", type(error).__name__)
        return error_response("SERVICE_UNAVAILABLE", "A required service is unavailable. Check server configuration and retry.", 503)

    async def one(collection, id):
        doc = await app.state.async_store.one(collection, {"id": id})
        if not doc:
            raise DomainError("NOT_FOUND", "The requested resource was not found.", 404)
        return doc

    async def graph_data(analysis_id):
        analysis = await one("analyses", analysis_id)
        if not analysis.get("graphReady"):
            raise DomainError("GRAPH_NOT_READY", "Static analysis is still preparing the graph.", 409)
        nodes, edges = await asyncio.gather(app.state.async_store.find("nodes", {"analysisId": analysis_id}), app.state.async_store.find("edges", {"analysisId": analysis_id}))
        return analysis, nodes, edges

    async def services():
        database = config.mode == "local"
        queue = config.mode == "local"
        if config.mode == "production":
            try:
                await app.state.async_store.client.admin.command("ping")
                database = True
            except Exception:
                pass
            try:
                import redis
                def ping():
                    with redis.Redis.from_url(config.redis_url, socket_timeout=1, socket_connect_timeout=1) as connection:
                        return bool(connection.ping())
                queue = await asyncio.to_thread(ping)
            except Exception:
                pass
        return {"database": database, "gemini": bool(config.gemini_api_key), "queue": queue}

    @app.get("/api/v1/capabilities", response_model=Capabilities, response_model_exclude_none=True)
    async def capabilities():
        return {"languages": [{"id": name, "label": label, "status": "supported" if name in {"javascript", "typescript"} else "planned", "capabilities": ["declarations", "imports", "exports", "references", "direct-calls", "semantic-chunks"] if name in {"javascript", "typescript"} else []} for name, label in (("javascript", "JavaScript"), ("typescript", "TypeScript"), ("python", "Python"), ("go", "Go"), ("rust", "Rust"), ("java", "Java"))], "services": await services(), "mode": config.mode}

    @app.get("/api/v1/repositories", response_model=RepositoryList, response_model_exclude_none=True)
    async def repositories():
        repos, analyses = await asyncio.gather(app.state.async_store.find("repositories", limit=100), app.state.async_store.find("analyses", limit=500))
        return {"repositories": repos, "analyses": sorted(analyses, key=lambda item: item["createdAt"], reverse=True)}

    async def submit(metadata, demo=False):
        async with app.state.admission:
            payload, created = await asyncio.to_thread(create_job, app.state.store, metadata, config, demo)
            if created or payload["job"]["status"] == "queued":
                try:
                    await asyncio.to_thread(launch, payload["job"]["id"], config)
                except Exception:
                    # Mongo state is durable; the reconciler can enqueue once Redis recovers.
                    await asyncio.to_thread(app.state.store.update, "jobs", {"id": payload["job"]["id"]}, {"message": "Job saved; waiting for the background queue to reconnect"})
            return JSONResponse(Submission.model_validate(payload).model_dump(exclude_none=True), status_code=202 if created else 200)

    @app.post("/api/v1/repositories", response_model=Submission, status_code=202)
    async def add_repository(body: SubmitRequest):
        metadata = await asyncio.to_thread(github_metadata, body.url, config)
        return await submit(metadata)

    @app.post("/api/v1/demo", response_model=Submission, status_code=202)
    async def demo():
        files, _ = await asyncio.to_thread(demo_files, config.demo_dir, "demo-hash", config)
        sha = hashlib.sha1("\n".join(file["path"] + file["contentHash"] for file in files).encode()).hexdigest()
        return await submit({"owner": "examples", "name": "beacon-store", "url": "local://beacon-store", "defaultBranch": "main", "commitSha": sha}, True)

    @app.get("/api/v1/analyses/{analysis_id}", response_model=Analysis, response_model_exclude_none=True)
    async def analysis(analysis_id: str):
        return await one("analyses", analysis_id)

    @app.get("/api/v1/jobs/{job_id}", response_model=Job, response_model_exclude_none=True)
    async def job(job_id: str):
        return await one("jobs", job_id)

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request):
        await one("jobs", job_id)
        async def events():
            previous = None
            while not await request.is_disconnected():
                current = await app.state.async_store.one("jobs", {"id": job_id})
                if not current:
                    break
                data = Job.model_validate(current).model_dump_json(exclude_none=True)
                if data != previous:
                    yield f"event: progress\ndata: {data}\n\n"
                    previous = data
                else:
                    yield ": heartbeat\n\n"
                if current["status"] in {"ready", "failed", "cancelled"}:
                    break
                await asyncio.sleep(1)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=Job, response_model_exclude_none=True)
    async def cancel_job(job_id: str):
        current = await one("jobs", job_id)
        if current["status"] in {"ready", "failed", "cancelled"}:
            return current
        changes = {"cancelRequested": True, "message": "Cancellation requested"}
        if current["status"] == "queued":
            changes.update(status="cancelled", phase="cancelled")
            await asyncio.to_thread(app.state.store.update, "analyses", {"id": current["analysisId"]}, {"status": "cancelled", "phase": "cancelled"})
        await asyncio.to_thread(app.state.store.update, "jobs", {"id": job_id}, changes)
        return await one("jobs", job_id)

    @app.post("/api/v1/jobs/{job_id}/retry", response_model=Job, response_model_exclude_none=True)
    async def retry_job(job_id: str, request: JobRetryRequest | None = None):
        current = await one("jobs", job_id)
        analysis = await one("analyses", current["analysisId"])
        retry_at = request.retryAt.astimezone(timezone.utc) if request and request.retryAt else None
        if retry_at and not datetime.now(timezone.utc) < retry_at <= datetime.now(timezone.utc) + timedelta(days=2):
            raise DomainError("INVALID_RETRY_TIME", "Choose a future retry time within the next two days.")
        if current["status"] in {"running", "queued"}:
            return current
        if current["status"] == "ready" and analysis.get("ragReady") and analysis.get("overview", {}).get("summary"):
            return current
        if analysis.get("ragReady") and not analysis.get("overview", {}).get("summary"):
            # Allow retrying a failed optional summary without redoing source or vectors.
            await asyncio.to_thread(app.state.store.update, "jobs", {"id": job_id}, {"checkpoints": [stage for stage in current.get("checkpoints", []) if stage != "summary"]})
        phase = "waiting_for_quota" if retry_at else "queued"
        await asyncio.to_thread(app.state.store.update, "jobs", {"id": job_id}, {"status": "queued", "phase": phase, "cancelRequested": False, "error": None, "nextAttemptAt": retry_at.isoformat() if retry_at else None, "message": "Saved embeddings are retained. Indexing resumes after the expected Gemini quota reset." if retry_at else "Resuming analysis from saved checkpoints", "heartbeatAt": now()})
        await asyncio.to_thread(app.state.store.update, "analyses", {"id": current["analysisId"]}, {"status": "queued", "phase": phase, "error": None})
        if not retry_at:
            await asyncio.to_thread(launch, job_id, config)
        return await one("jobs", job_id)

    @app.get("/api/v1/analyses/{analysis_id}/tree", response_model=TreeResponse, response_model_exclude_none=True)
    async def tree(analysis_id: str, parent: str = ""):
        await one("analyses", analysis_id)
        parent = parent.strip("/")
        if ".." in PurePosixPath(parent).parts or "\\" in parent:
            raise DomainError("INVALID_PATH", "Invalid source directory.")
        prefix = parent + "/" if parent else ""
        files = await app.state.async_store.find("files", {"analysisId": analysis_id}, projection={"content": 0})
        directories, entries = {}, []
        for file in files:
            if not file["path"].startswith(prefix):
                continue
            relative = file["path"][len(prefix):]
            if "/" in relative:
                name = relative.split("/", 1)[0]
                path = prefix + name
                directories.setdefault(path, {"id": f"module:{path}", "path": path, "name": name, "kind": "directory", "childCount": 0})["childCount"] += 1
            else:
                entries.append({"id": file["id"], "path": file["path"], "name": relative, "kind": "file", "language": file["language"]})
        return {"entries": sorted(directories.values(), key=lambda item: item["name"]) + sorted(entries, key=lambda item: item["name"])}

    @app.get("/api/v1/analyses/{analysis_id}/files/{file_id:path}", response_model=SourceFile, response_model_exclude_none=True)
    async def source(analysis_id: str, file_id: str):
        await one("analyses", analysis_id)
        file = await app.state.async_store.one("files", {"id": file_id, "analysisId": analysis_id})
        if not file:
            raise DomainError("FILE_NOT_FOUND", "This file does not belong to the requested analysis.", 404)
        return file

    @app.get("/api/v1/analyses/{analysis_id}/search", response_model=SearchResponse, response_model_exclude_none=True)
    async def search(analysis_id: str, q: str = Query(default="", max_length=256), offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        await one("analyses", analysis_id)
        nodes = await app.state.async_store.find("nodes", {"analysisId": analysis_id})
        query = q.casefold().strip()
        results = [node for node in nodes if node["kind"] in {"file", "symbol"} and (not query or query in node["name"].casefold() or query in node.get("path", "").casefold())]
        results.sort(key=lambda node: (node["name"].casefold() != query, node.get("path", ""), node["name"]))
        return {"results": [{key: node[key] for key in ("id", "kind", "name", "path", "startLine", "symbolKind") if key in node} for node in results[offset:offset + limit]], "total": len(results)}

    @app.get("/api/v1/analyses/{analysis_id}/graph", response_model=GraphData, response_model_exclude_none=True)
    async def graph(analysis_id: str, level: Literal["modules", "files", "symbols"] = "modules", focus: str | None = None, direction: Literal["both", "dependencies", "dependents"] = "both", depth: int = Query(default=1, ge=1, le=2), language: str | None = None, kind: str | None = None):
        _, nodes, edges = await graph_data(analysis_id)
        return await asyncio.to_thread(project_graph, nodes, edges, level, focus, direction, depth, language, kind)

    @app.post("/api/v1/analyses/{analysis_id}/chat", response_model=ChatResponse, response_model_exclude_none=True)
    async def chat(analysis_id: str, body: ChatRequest):
        analysis, nodes, edges = await graph_data(analysis_id)
        if not body.message.strip() and not body.action:
            raise DomainError("QUESTION_REQUIRED", "Enter a question or choose an exploration action.")
        if body.action in {"dependencies", "dependents"}:
            files = await app.state.async_store.find("files", {"analysisId": analysis_id}, projection={"content": 0})
            return dependency_answer(analysis, nodes, edges, files, body.selectedNodeId, body.action)
        if app.state.chat_slots.locked():
            raise DomainError("CHAT_BUSY", "Two answers are already being generated. Retry shortly.", 429)
        async with app.state.chat_slots:
            return await asyncio.to_thread(answer_question, app.state.store, analysis, body, config)

    @app.delete("/api/v1/repositories/{repository_id}", status_code=204)
    async def remove_repository(repository_id: str):
        await one("repositories", repository_id)
        # Wait for workers to acknowledge cancellation before removing source artifacts.
        analyses = await app.state.async_store.find("analyses", {"repositoryId": repository_id})
        for analysis in analyses:
            await cancel_job(analysis["jobId"])
        deadline = time.monotonic() + 10
        while True:
            active = [job for job in await app.state.async_store.find("jobs") if job.get("analysisId") in {analysis["id"] for analysis in analyses} and job["status"] == "running"]
            if not active:
                break
            if time.monotonic() >= deadline:
                raise DomainError("CANCELLATION_PENDING", "Cancellation is in progress. Retry deletion once the current network operation finishes.", 409)
            await asyncio.sleep(0.2)
        await asyncio.to_thread(delete_repository, app.state.store, repository_id)
        return Response(status_code=204)

    @app.get("/api/v1/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    async def ready():
        state = await services()
        healthy = state["database"] and state["queue"]
        return JSONResponse({"status": "ready" if healthy else "unavailable", "services": state, "mode": config.mode}, status_code=200 if healthy else 503)

    return app


def error_response(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


app = create_app()
