"""Pre-index the example and cache its public answers before opening the demo.

Run in the API container: python -m navigator.prewarm_demo
"""
import hashlib
import time

from .config import get_settings
from .contracts import ChatRequest, DomainError
from .demo import QUESTIONS, answer_id, routes_answer
from .ingestion import demo_files
from .jobs import create_job, launch
from .quota import QuotaGate
from .rag import answer_question
from .storage import make_store


def main():
    config = get_settings()
    store = make_store(config)
    quota = None
    try:
        files, _ = demo_files(config.demo_dir, "demo-hash", config)
        sha = hashlib.sha1("\n".join(file["path"] + file["contentHash"] for file in files).encode()).hexdigest()
        payload, created = create_job(store, {"owner": "examples", "name": "beacon-store",
            "url": "local://beacon-store", "defaultBranch": "main", "commitSha": sha}, config, demo=True)
        analysis_id = payload["analysis"]["id"]
        job = payload["job"]
        if job["status"] in {"failed", "cancelled"}:
            store.update("jobs", {"id": job["id"]}, {"status": "queued", "phase": "queued",
                "cancelRequested": False, "error": None, "nextAttemptAt": None,
                "message": "Preparing the example repository"})
            store.update("analyses", {"id": analysis_id}, {"status": "queued", "error": None})
        if created or job["status"] in {"queued", "failed", "cancelled"}:
            launch(payload["job"]["id"], config)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            analysis = store.one("analyses", {"id": analysis_id})
            if analysis and analysis.get("ragReady"):
                break
            if analysis and analysis["status"] in {"failed", "cancelled"}:
                raise RuntimeError(f"Demo analysis stopped: {analysis.get('error')}")
            time.sleep(3)
        else:
            raise TimeoutError("Demo analysis did not become AI-ready within 20 minutes")
        if config.effective_answer_provider == "gemini":
            quota = QuotaGate(config)
        for question in QUESTIONS:
            key = answer_id(analysis_id, question)
            if store.one("demo_answers", {"id": key}):
                continue
            if question == "Where are the API routes?":
                answer = routes_answer(store, analysis)
                store.put("demo_answers", {"id": key, "analysisId": analysis_id,
                                           "question": question, "answer": answer})
                continue
            answer_deadline = time.monotonic() + 900
            while True:
                try:
                    answer = answer_question(store, analysis, ChatRequest(message=question), config, quota)
                    break
                except DomainError as error:
                    delay = error.retry_after_seconds or 30
                    if error.status != 429 or time.monotonic() + delay > answer_deadline:
                        raise
                    print(f"Answer capacity paused; retrying in {int(delay)} seconds", flush=True)
                    time.sleep(max(1, delay))
            store.put("demo_answers", {"id": key, "analysisId": analysis_id,
                                       "question": question, "answer": answer})
        print(f"Demo ready: {analysis_id}; {len(QUESTIONS)} prepared answers")
    finally:
        if quota:
            quota.close()
        store.close()


if __name__ == "__main__":
    main()
