import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
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
from .. import auth
from ..contracts import ChatRequest, DomainError, JobRetryRequest, SubmitRequest
from ..demo import answer_id
from ..graph import project_graph
from ..ingestion import demo_files, github_metadata
from ..jobs import create_job, delete_repository, launch, now, recover_jobs
from ..rag import answer_question, dependency_answer, stream_answer_question
from ..quota import QuotaGate
from ..storage import AsyncStore, create_indexes, make_store
from ..responses import Analysis, Capabilities, ChatResponse, GraphData, Job, RepositoryList, SearchResponse, SourceFile, Submission, TreeResponse

logger = logging.getLogger(__name__)


def create_app(config: Settings | None = None):
    config = config or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = make_store(config)
        app.state.async_store = None
        app.state.quota = QuotaGate(config) if config.mode == "production" else None
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
            if app.state.quota:
                app.state.quota.close()
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
            if config.mode == "production" and (request.cookies.get(auth.SESSION_COOKIE) or request.cookies.get(auth.GUEST_COOKIE)) and not origin:
                return error_response("ORIGIN_REQUIRED", "This authenticated request needs a trusted origin.", 403)
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
            limit = 30 if request.url.path.endswith(("/chat", "/chat/stream")) else 12
            if len(attempts) >= limit:
                return error_response("RATE_LIMITED", "Too many requests. Please retry in a minute.", 429)
            attempts.append(current)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request, error):
        return error_response(error.code, error.message, error.status, error.retry_after_seconds)

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

    async def authorized_analysis(analysis_id, request, *, write=False):
        analysis = await one("analyses", analysis_id)
        if config.mode == "local":
            return analysis
        if analysis.get("demo") and not write:
            return analysis
        user = await asyncio.to_thread(auth.require_identity, app.state.store, request, config)
        if analysis.get("demo") or not await app.state.async_store.one(
                "user_analyses", {"id": f"{user['id']}:{analysis_id}"}):
            raise DomainError("NOT_FOUND", "The requested resource was not found.", 404)
        return analysis

    async def authorized_job(job_id, request, *, write=False):
        job = await one("jobs", job_id)
        await authorized_analysis(job["analysisId"], request, write=write)
        return job

    async def graph_data(analysis_id, request):
        analysis = await authorized_analysis(analysis_id, request)
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
        language_caps = {"javascript": ["declarations", "imports", "exports", "references", "direct-calls", "semantic-chunks"],
                         "typescript": ["declarations", "imports", "exports", "references", "direct-calls", "semantic-chunks"],
                         "python": ["declarations", "imports", "local-calls", "framework-routes", "semantic-chunks"],
                         "java": ["declarations", "imports", "spring-routes", "semantic-chunks"]}
        return {"languages": [{"id": name, "label": label, "status": "supported" if name in language_caps else "planned", "capabilities": language_caps.get(name, [])} for name, label in (("javascript", "JavaScript"), ("typescript", "TypeScript"), ("python", "Python"), ("go", "Go"), ("rust", "Rust"), ("java", "Java"))], "services": await services(), "mode": config.mode}

    @app.get("/api/v1/auth/github/start")
    async def github_login():
        return await asyncio.to_thread(auth.start_login, app.state.store, config)

    @app.get("/api/v1/auth/github/callback")
    async def github_callback(request: Request, code: str = "", state: str = ""):
        return await asyncio.to_thread(auth.finish_login, app.state.store, config,
                                       state, request.cookies.get(auth.STATE_COOKIE), code,
                                       request.cookies.get(auth.GUEST_COOKIE))

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request):
        return await asyncio.to_thread(auth.logout, app.state.store, request, config)

    @app.get("/api/v1/auth/me")
    async def me(request: Request, response: Response):
        user = await asyncio.to_thread(auth.current_user, app.state.store, request, config)
        if user:
            return {"authenticated": True, "user": {"id": user["id"], "login": user["login"],
                "avatarUrl": user.get("avatarUrl", "")},
                "questionsRemaining": None, "freeRepositoryUsed": False, "limitsApply": False}
        guest = await asyncio.to_thread(auth.ensure_guest, app.state.store, request, response, config)
        return {"authenticated": False, "user": None,
                "questionsRemaining": max(0, config.free_questions - guest.get("questionsUsed", 0)),
                "freeRepositoryUsed": bool(guest.get("freeRepositoryId")),
                "limitsApply": config.mode == "production"}

    @app.get("/api/v1/repositories", response_model=RepositoryList, response_model_exclude_none=True)
    async def repositories(request: Request):
        if config.mode == "production":
            user = await asyncio.to_thread(auth.current_identity, app.state.store, request, config)
            if not user:
                return {"repositories": [], "analyses": []}
            grants = await app.state.async_store.find("user_analyses", {"userId": user["id"]}, limit=100)
            analyses = []
            for grant in grants:
                analysis = await app.state.async_store.one("analyses", {"id": grant["analysisId"]})
                if analysis:
                    analyses.append(analysis)
            analyses.sort(key=lambda item: item["createdAt"], reverse=True)
            repos = []
            seen_repositories = set()
            for analysis in analyses:
                if analysis["repositoryId"] in seen_repositories:
                    continue
                repo = await app.state.async_store.one("repositories", {"id": analysis["repositoryId"]})
                if repo:
                    repos.append({**repo, "latestAnalysisId": analysis["id"]})
                    seen_repositories.add(analysis["repositoryId"])
            return {"repositories": repos, "analyses": analyses}
        repos, analyses = await asyncio.gather(app.state.async_store.find("repositories", limit=100), app.state.async_store.find("analyses", limit=500))
        return {"repositories": repos, "analyses": sorted(analyses, key=lambda item: item["createdAt"], reverse=True)}

    async def submit(metadata, demo=False, preview=False, user=None):
        async with app.state.admission:
            payload, created = await asyncio.to_thread(create_job, app.state.store, metadata, config, demo, preview)
            if user:
                await asyncio.to_thread(app.state.store.ensure, "user_analyses", {
                    "id": f"{user['id']}:{payload['analysis']['id']}", "userId": user["id"],
                    "analysisId": payload["analysis"]["id"], "repositoryId": payload["repository"]["id"],
                    "createdAt": now()})
            if created or payload["job"]["status"] == "queued":
                try:
                    await asyncio.to_thread(launch, payload["job"]["id"], config)
                except Exception:
                    # Mongo state is durable; the reconciler can enqueue once Redis recovers.
                    await asyncio.to_thread(app.state.store.update, "jobs", {"id": payload["job"]["id"]}, {"message": "Job saved; waiting for the background queue to reconnect"})
            return JSONResponse(Submission.model_validate(payload).model_dump(exclude_none=True), status_code=202 if created else 200)

    async def check_submission(request):
        if app.state.quota:
            # Nginx replaces X-Real-IP. The API itself is bound to loopback on
            # the host, so direct external clients cannot supply this header.
            ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
            await asyncio.to_thread(app.state.quota.submission, ip)

    @app.post("/api/v1/repositories", response_model=Submission, status_code=202)
    async def add_repository(body: SubmitRequest, request: Request, response: Response):
        user = await asyncio.to_thread(auth.current_user, app.state.store, request, config)
        if not user and config.mode == "production":
            user = await asyncio.to_thread(auth.ensure_guest, app.state.store, request, response, config)
        metadata = await asyncio.to_thread(github_metadata, body.url, config)
        preview = config.mode == "production" and bool(user and user.get("guest"))
        profile = config.preview_analysis_profile if preview else config.analysis_profile
        analysis_id = hashlib.sha256(f"github-{metadata['githubId']}:{metadata['commitSha']}:{profile}".encode()).hexdigest()[:24]
        if not await app.state.async_store.one("analyses", {"id": analysis_id}):
            await check_submission(request)
        if preview and not await asyncio.to_thread(
                app.state.store.claim_free_repository, user["id"], f"github-{metadata['githubId']}", analysis_id):
            raise DomainError("FREE_REPOSITORY_USED", "Your guest preview has already been used. Sign in for full access.", 403)
        result = await submit(metadata, preview=preview, user=user if config.mode == "production" else None)
        result.raw_headers.extend((name, value) for name, value in response.raw_headers if name == b"set-cookie")
        return result

    @app.post("/api/v1/repositories/restore", response_model=Submission, response_model_exclude_none=True)
    async def restore_repository(request: Request):
        user = await asyncio.to_thread(auth.require_identity, app.state.store, request, config)
        if not user.get("guest"):
            raise DomainError("NOT_FOUND", "No guest preview is available to restore.", 404)
        analysis_id = user.get("freeAnalysisId")
        if not analysis_id:
            raise DomainError("NOT_FOUND", "No saved free preview is available to restore.", 404)
        analysis = await one("analyses", analysis_id)
        repository, job = await asyncio.gather(
            one("repositories", analysis["repositoryId"]), one("jobs", analysis["jobId"]))
        await asyncio.to_thread(app.state.store.ensure, "user_analyses", {
            "id": f"{user['id']}:{analysis_id}", "userId": user["id"],
            "analysisId": analysis_id, "repositoryId": repository["id"], "createdAt": now()})
        return {"repository": repository, "analysis": analysis, "job": job}

    @app.post("/api/v1/analyses/{analysis_id}/upgrade", response_model=Submission, response_model_exclude_none=True)
    async def upgrade_preview(analysis_id: str, request: Request):
        user = await asyncio.to_thread(auth.require_user, app.state.store, request, config)
        analysis = await authorized_analysis(analysis_id, request)
        if not analysis.get("preview") or analysis.get("demo"):
            raise DomainError("NOT_A_PREVIEW", "Only a repository preview can be upgraded.", 409)
        repository = await one("repositories", analysis["repositoryId"])
        metadata = {key: repository[key] for key in ("owner", "name", "url", "defaultBranch", "githubId")}
        metadata["commitSha"] = analysis["commitSha"]
        full_id = hashlib.sha256(f"github-{repository['githubId']}:{analysis['commitSha']}:{config.analysis_profile}".encode()).hexdigest()[:24]
        if not await app.state.async_store.one("analyses", {"id": full_id}):
            await check_submission(request)
        return await submit(metadata, preview=False, user=user)

    async def demo_payload(request: Request):
        files, _ = await asyncio.to_thread(demo_files, config.demo_dir, "demo-hash", config)
        sha = hashlib.sha1("\n".join(file["path"] + file["contentHash"] for file in files).encode()).hexdigest()
        analysis_id = hashlib.sha256(f"demo-beacon-store:{sha}:{config.analysis_profile}".encode()).hexdigest()[:24]
        if config.mode == "production":
            analysis = await app.state.async_store.one("analyses", {"id": analysis_id})
            if not analysis or not analysis.get("ragReady"):
                raise DomainError("DEMO_NOT_READY", "The example repository is being prepared. Try again shortly.", 503)
            repo, job = await asyncio.gather(
                app.state.async_store.one("repositories", {"id": "demo-beacon-store"}),
                app.state.async_store.one("jobs", {"id": analysis["jobId"]}))
            return {"repository": repo, "analysis": analysis, "job": job}
        if not await app.state.async_store.one("analyses", {"id": analysis_id}):
            await check_submission(request)
        return await submit({"owner": "examples", "name": "beacon-store", "url": "local://beacon-store", "defaultBranch": "main", "commitSha": sha}, True)

    @app.get("/api/v1/demo", response_model=Submission, response_model_exclude_none=True)
    async def demo_get(request: Request):
        return await demo_payload(request)

    @app.post("/api/v1/demo", response_model=Submission, status_code=202)
    async def demo(request: Request):
        return await demo_payload(request)

    @app.get("/api/v1/analyses/{analysis_id}", response_model=Analysis, response_model_exclude_none=True)
    async def analysis(analysis_id: str, request: Request):
        return await authorized_analysis(analysis_id, request)

    @app.get("/api/v1/jobs/{job_id}", response_model=Job, response_model_exclude_none=True)
    async def job(job_id: str, request: Request):
        return await authorized_job(job_id, request)

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request):
        await authorized_job(job_id, request)
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
    async def cancel_job(job_id: str, request: Request):
        current = await authorized_job(job_id, request, write=True)
        if current["status"] in {"ready", "failed", "cancelled"}:
            return current
        changes = {"cancelRequested": True, "message": "Cancellation requested"}
        if current["status"] == "queued":
            changes.update(status="cancelled", phase="cancelled")
            await asyncio.to_thread(app.state.store.update, "analyses", {"id": current["analysisId"]}, {"status": "cancelled", "phase": "cancelled"})
        await asyncio.to_thread(app.state.store.update, "jobs", {"id": job_id}, changes)
        return await one("jobs", job_id)

    @app.post("/api/v1/jobs/{job_id}/retry", response_model=Job, response_model_exclude_none=True)
    async def retry_job(job_id: str, request: Request, body: JobRetryRequest | None = None):
        current = await authorized_job(job_id, request, write=True)
        analysis = await one("analyses", current["analysisId"])
        retry_at = body.retryAt.astimezone(timezone.utc) if body and body.retryAt else None
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
        await asyncio.to_thread(app.state.store.update, "jobs", {"id": job_id}, {"status": "queued", "phase": phase, "cancelRequested": False, "error": None, "nextAttemptAt": retry_at.isoformat() if retry_at else None, "message": "Saved embeddings are retained. Indexing resumes at the requested time." if retry_at else "Resuming analysis from saved checkpoints", "heartbeatAt": now()})
        await asyncio.to_thread(app.state.store.update, "analyses", {"id": current["analysisId"]}, {"status": "queued", "phase": phase, "error": None})
        if not retry_at:
            await asyncio.to_thread(launch, job_id, config)
        return await one("jobs", job_id)

    @app.get("/api/v1/analyses/{analysis_id}/tree", response_model=TreeResponse, response_model_exclude_none=True)
    async def tree(analysis_id: str, request: Request, parent: str = ""):
        await authorized_analysis(analysis_id, request)
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
    async def source(analysis_id: str, file_id: str, request: Request):
        await authorized_analysis(analysis_id, request)
        file = await app.state.async_store.one("files", {"id": file_id, "analysisId": analysis_id})
        if not file:
            raise DomainError("FILE_NOT_FOUND", "This file does not belong to the requested analysis.", 404)
        return file

    @app.get("/api/v1/analyses/{analysis_id}/search", response_model=SearchResponse, response_model_exclude_none=True)
    async def search(analysis_id: str, request: Request, q: str = Query(default="", max_length=256), offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        await authorized_analysis(analysis_id, request)
        nodes = await app.state.async_store.find("nodes", {"analysisId": analysis_id})
        query = q.casefold().strip()
        results = [node for node in nodes if node["kind"] in {"file", "symbol"} and (not query or query in node["name"].casefold() or query in node.get("path", "").casefold())]
        results.sort(key=lambda node: (node["name"].casefold() != query, node.get("path", ""), node["name"]))
        return {"results": [{key: node[key] for key in ("id", "kind", "name", "path", "startLine", "symbolKind") if key in node} for node in results[offset:offset + limit]], "total": len(results)}

    @app.get("/api/v1/analyses/{analysis_id}/graph", response_model=GraphData, response_model_exclude_none=True)
    async def graph(analysis_id: str, request: Request, level: Literal["modules", "files", "symbols"] = "modules", focus: str | None = None, direction: Literal["both", "dependencies", "dependents"] = "both", depth: int = Query(default=1, ge=1, le=2), language: str | None = None, kind: str | None = None):
        _, nodes, edges = await graph_data(analysis_id, request)
        return await asyncio.to_thread(project_graph, nodes, edges, level, focus, direction, depth, language, kind)

    async def reserve_answer(request):
        if config.mode == "local":
            return None
        if await asyncio.to_thread(auth.current_user, app.state.store, request, config):
            return None
        user = await asyncio.to_thread(auth.require_identity, app.state.store, request, config)
        if not await asyncio.to_thread(app.state.store.reserve_question, user["id"], config.free_questions):
            raise DomainError("FREE_QUESTIONS_USED", "Your guest questions have been used. Sign in for full access.", 403)
        return user["id"]

    async def prepared_demo_answer(analysis, body):
        if not analysis.get("demo") or body.action or body.selectedNodeId:
            return None
        saved = await app.state.async_store.one("demo_answers", {"id": answer_id(analysis["id"], body.message)})
        return saved["answer"] if saved else None

    async def save_chat_turn(request, analysis_id, body, answer):
        if config.mode == "local":
            return
        user = await asyncio.to_thread(auth.current_identity, app.state.store, request, config)
        if user:
            await asyncio.to_thread(app.state.store.put, "chat_turns", {
                "id": uuid.uuid4().hex, "userId": user["id"], "analysisId": analysis_id,
                "question": body.message, "answer": answer, "createdAt": now()})

    @app.get("/api/v1/analyses/{analysis_id}/chat/history")
    async def chat_history(analysis_id: str, request: Request):
        await authorized_analysis(analysis_id, request)
        user = await asyncio.to_thread(auth.current_identity, app.state.store, request, config)
        if not user or config.mode == "local":
            return {"turns": []}
        turns = await app.state.async_store.find("chat_turns", {"userId": user["id"],
                                                            "analysisId": analysis_id}, limit=100)
        return {"turns": sorted(turns, key=lambda item: item["createdAt"])[-50:]}

    @app.delete("/api/v1/analyses/{analysis_id}/chat/history", status_code=204)
    async def clear_chat_history(analysis_id: str, request: Request):
        await authorized_analysis(analysis_id, request)
        user = await asyncio.to_thread(auth.require_identity, app.state.store, request, config)
        if config.mode == "production":
            await asyncio.to_thread(app.state.store.delete, "chat_turns", {"userId": user["id"],
                                                                            "analysisId": analysis_id})
        return Response(status_code=204)

    @app.post("/api/v1/analyses/{analysis_id}/chat", response_model=ChatResponse, response_model_exclude_none=True)
    async def chat(analysis_id: str, body: ChatRequest, request: Request):
        analysis, nodes, edges = await graph_data(analysis_id, request)
        if not body.message.strip() and not body.action:
            raise DomainError("QUESTION_REQUIRED", "Enter a question or choose an exploration action.")
        if body.action in {"dependencies", "dependents"}:
            files = await app.state.async_store.find("files", {"analysisId": analysis_id}, projection={"content": 0})
            answer = dependency_answer(analysis, nodes, edges, files, body.selectedNodeId, body.action)
            await save_chat_turn(request, analysis_id, body, answer)
            return answer
        prepared = await prepared_demo_answer(analysis, body)
        if prepared:
            await save_chat_turn(request, analysis_id, body, prepared)
            return prepared
        if app.state.chat_slots.locked():
            raise DomainError("CHAT_BUSY", "Two answers are already being generated. Retry shortly.", 429, retry_after_seconds=5)
        async with app.state.chat_slots:
            user_id = await reserve_answer(request)
            analysis_config = config.for_analysis_profile(analysis["profile"])
            try:
                answer = await asyncio.to_thread(answer_question, app.state.store, analysis, body, analysis_config, app.state.quota)
                await save_chat_turn(request, analysis_id, body, answer)
                return answer
            except Exception:
                if user_id:
                    await asyncio.to_thread(app.state.store.refund_question, user_id)
                raise

    @app.post("/api/v1/analyses/{analysis_id}/chat/stream")
    async def chat_stream(analysis_id: str, body: ChatRequest, request: Request):
        analysis, nodes, edges = await graph_data(analysis_id, request)
        if not body.message.strip() and not body.action:
            raise DomainError("QUESTION_REQUIRED", "Enter a question or choose an exploration action.")
        graph_action = body.action in {"dependencies", "dependents"}
        if graph_action:
            files = await app.state.async_store.find("files", {"analysisId": analysis_id}, projection={"content": 0})
            answer = dependency_answer(analysis, nodes, edges, files, body.selectedNodeId, body.action)
        else:
            answer = await prepared_demo_answer(analysis, body)
        if not graph_action and not answer and app.state.chat_slots.locked():
            raise DomainError("CHAT_BUSY", "Two answers are already being generated. Retry shortly.", 429, retry_after_seconds=5)
        user_id = await reserve_answer(request) if not graph_action and not answer else None

        def frame(event):
            return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"

        def next_event(iterator):
            try:
                return next(iterator)
            except StopIteration:
                return None

        async def events():
            if answer:
                await save_chat_turn(request, analysis_id, body, answer)
                for block in answer["blocks"]:
                    yield frame({"type": "block", "block": block, "citations": [citation for citation in answer["citations"] if citation["id"] in block["citationIds"]]})
                yield frame({"type": "done", "answer": answer})
                return
            async with app.state.chat_slots:
                analysis_config = config.for_analysis_profile(analysis["profile"])
                iterator = iter(stream_answer_question(app.state.store, analysis, body, analysis_config, app.state.quota))
                try:
                    while not await request.is_disconnected():
                        event = await asyncio.to_thread(next_event, iterator)
                        if event is None:
                            break
                        if event["type"] == "done":
                            await save_chat_turn(request, analysis_id, body, event["answer"])
                        yield frame(event)
                except DomainError as error:
                    if user_id:
                        await asyncio.to_thread(app.state.store.refund_question, user_id)
                    yield frame({"type": "error", "message": error.message, "code": error.code, "retryAfter": error.retry_after_seconds})
                except Exception as error:
                    if user_id:
                        await asyncio.to_thread(app.state.store.refund_question, user_id)
                    logger.error("Chat stream failed: %s", type(error).__name__)
                    yield frame({"type": "error", "message": "A required service is unavailable. Check server configuration and retry.", "code": "SERVICE_UNAVAILABLE"})
                finally:
                    await asyncio.to_thread(iterator.close)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.delete("/api/v1/repositories/{repository_id}", status_code=204)
    async def remove_repository(repository_id: str, request: Request):
        if config.mode == "production":
            user = await asyncio.to_thread(auth.require_identity, app.state.store, request, config)
            grants = await app.state.async_store.find("user_analyses", {"userId": user["id"], "repositoryId": repository_id})
            if not grants:
                raise DomainError("NOT_FOUND", "The requested resource was not found.", 404)
            analyses = [await one("analyses", grant["analysisId"]) for grant in grants]
            jobs = [await one("jobs", analysis["jobId"]) for analysis in analyses]
            if any(job["status"] in {"running", "queued"} for job in jobs):
                raise DomainError("ANALYSIS_ACTIVE", "Cancel the analysis before removing this repository.", 409)
            await asyncio.to_thread(app.state.store.delete, "user_analyses", {"userId": user["id"], "repositoryId": repository_id})
            for grant in grants:
                await asyncio.to_thread(app.state.store.delete, "chat_turns", {"userId": user["id"], "analysisId": grant["analysisId"]})
            if user.get("guest"):
                for analysis in analyses:
                    if analysis["status"] in {"failed", "cancelled"} and not analysis.get("graphReady"):
                        await asyncio.to_thread(app.state.store.release_failed_repository, user["id"], analysis["id"])
            return Response(status_code=204)
        await one("repositories", repository_id)
        # Wait for workers to acknowledge cancellation before removing source artifacts.
        analyses = await app.state.async_store.find("analyses", {"repositoryId": repository_id})
        for analysis in analyses:
            await cancel_job(analysis["jobId"], request)
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


def error_response(code, message, status, retry_after=None):
    headers = {"Retry-After": str(max(1, math.ceil(retry_after)))} if retry_after and status == 429 else None
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


app = create_app()
