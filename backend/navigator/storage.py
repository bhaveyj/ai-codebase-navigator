"""Small collection interface shared by durable workers and the async API.

Local mode is an explicit single-process development mode. Production uses
MongoDB; source data never silently falls back to disk on connection failure.
"""
import asyncio
import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from pymongo import AsyncMongoClient, MongoClient, ReplaceOne, ReturnDocument
from pymongo.operations import SearchIndexModel

from .config import Settings

SCOPED = {"files", "nodes", "edges", "regions", "unresolved_references", "diagnostics", "chunks"}
_local_locks: dict[str, threading.RLock] = {}


def public(doc):
    return {key: value for key, value in doc.items() if key != "_id"} if doc else None


def matches(doc: dict, query: dict) -> bool:
    for key, value in query.items():
        actual = doc.get(key)
        if isinstance(value, dict):
            if "$in" in value and actual not in value["$in"]:
                return False
            if "$ne" in value and actual == value["$ne"]:
                return False
        elif actual != value:
            return False
    return True


class LocalStore:
    is_mongo = False

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = _local_locks.setdefault(str(directory.resolve()), threading.RLock())

    def _read(self, collection):
        path = self.directory / f"{collection}.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else []

    def _write(self, collection, docs):
        path = self.directory / f"{collection}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(docs, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def find(self, collection, query=None, limit=200000, projection=None):
        with self.lock:
            found = deepcopy([d for d in self._read(collection) if matches(d, query or {})][:limit])
            if projection:
                found = [{key: value for key, value in doc.items() if projection.get(key, 1) != 0} for doc in found]
            return found

    def one(self, collection, query):
        items = self.find(collection, query, 1)
        return items[0] if items else None

    def put(self, collection, doc):
        self.put_many(collection, [doc])

    def ensure(self, collection, doc):
        with self.lock:
            found = self.one(collection, {"id": doc["id"]})
            if found:
                return found
            self.put(collection, doc)
            return doc

    def put_many(self, collection, documents):
        with self.lock:
            docs = self._read(collection)
            by_key = {(d.get("analysisId"), d["id"]): d for d in docs}
            for doc in documents:
                by_key[(doc.get("analysisId"), doc["id"])] = deepcopy(doc)
            self._write(collection, list(by_key.values()))

    def update(self, collection, query, changes):
        with self.lock:
            docs = self._read(collection)
            count = 0
            for doc in docs:
                if matches(doc, query):
                    doc.update(deepcopy(changes))
                    count += 1
            self._write(collection, docs)
            return count

    def delete(self, collection, query):
        with self.lock:
            self._write(collection, [d for d in self._read(collection) if not matches(d, query)])

    def pop(self, collection, query):
        with self.lock:
            found = self.one(collection, query)
            if found:
                self.delete(collection, query)
            return found

    def claim_free_repository(self, user_id, repository_id, analysis_id):
        with self.lock:
            user = self.one("users", {"id": user_id})
            if not user or user.get("freeAnalysisId") not in (None, analysis_id):
                return False
            self.update("users", {"id": user_id}, {"freeRepositoryId": repository_id,
                                                    "freeAnalysisId": analysis_id})
            return True

    def release_failed_repository(self, user_id, analysis_id):
        with self.lock:
            user = self.one("users", {"id": user_id})
            if not user or user.get("freeAnalysisId") != analysis_id:
                return False
            self.update("users", {"id": user_id}, {"freeRepositoryId": None,
                                                    "freeAnalysisId": None})
            return True

    def reserve_question(self, user_id, limit):
        with self.lock:
            user = self.one("users", {"id": user_id})
            if not user or user.get("questionsUsed", 0) >= limit:
                return False
            self.update("users", {"id": user_id}, {"questionsUsed": user.get("questionsUsed", 0) + 1})
            return True

    def refund_question(self, user_id):
        with self.lock:
            user = self.one("users", {"id": user_id})
            if user and user.get("questionsUsed", 0) > 0:
                self.update("users", {"id": user_id}, {"questionsUsed": user["questionsUsed"] - 1})

    def close(self):
        pass


class MongoStore:
    is_mongo = True

    def __init__(self, settings: Settings):
        if not settings.mongodb_uri:
            raise RuntimeError("Production mode requires MONGODB_URI. Set NAVIGATOR_MODE=local for disk-backed graph exploration.")
        self.client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[settings.mongodb_database]

    def find(self, collection, query=None, limit=200000, projection=None):
        return [public(d) for d in self.db[collection].find(query or {}, projection).limit(limit)]

    def one(self, collection, query):
        return public(self.db[collection].find_one(query))

    def put(self, collection, doc):
        key = {"id": doc["id"]}
        if collection in SCOPED:
            key["analysisId"] = doc["analysisId"]
        self.db[collection].replace_one(key, doc, upsert=True)

    def ensure(self, collection, doc):
        self.db[collection].update_one({"id": doc["id"]}, {"$setOnInsert": doc}, upsert=True)
        return self.one(collection, {"id": doc["id"]})

    def put_many(self, collection, documents):
        operations = []
        for doc in documents:
            key = {"id": doc["id"]}
            if collection in SCOPED:
                key["analysisId"] = doc["analysisId"]
            operations.append(ReplaceOne(key, doc, upsert=True))
        if operations:
            self.db[collection].bulk_write(operations, ordered=False)

    def update(self, collection, query, changes):
        return self.db[collection].update_many(query, {"$set": changes}).matched_count

    def delete(self, collection, query):
        self.db[collection].delete_many(query)

    def pop(self, collection, query):
        return public(self.db[collection].find_one_and_delete(query))

    def claim_free_repository(self, user_id, repository_id, analysis_id):
        return self.db.users.find_one_and_update(
            {"id": user_id, "$or": [{"freeAnalysisId": {"$exists": False}},
                                     {"freeAnalysisId": analysis_id}]},
            {"$set": {"freeRepositoryId": repository_id, "freeAnalysisId": analysis_id}},
            return_document=ReturnDocument.AFTER) is not None

    def release_failed_repository(self, user_id, analysis_id):
        return self.db.users.find_one_and_update(
            {"id": user_id, "freeAnalysisId": analysis_id},
            {"$unset": {"freeRepositoryId": "", "freeAnalysisId": ""}},
            return_document=ReturnDocument.AFTER) is not None

    def reserve_question(self, user_id, limit):
        return self.db.users.find_one_and_update(
            {"id": user_id, "questionsUsed": {"$lt": limit}},
            {"$inc": {"questionsUsed": 1}},
            return_document=ReturnDocument.AFTER) is not None

    def refund_question(self, user_id):
        self.db.users.update_one({"id": user_id, "questionsUsed": {"$gt": 0}},
                                 {"$inc": {"questionsUsed": -1}})

    def close(self):
        self.client.close()


class AsyncStore:
    def __init__(self, settings, local=None):
        self.local = local
        self.client = None if local else AsyncMongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
        self.db = None if local else self.client[settings.mongodb_database]

    async def one(self, collection, query):
        if self.local:
            return await asyncio.to_thread(self.local.one, collection, query)
        return public(await self.db[collection].find_one(query))

    async def find(self, collection, query=None, limit=200000, projection=None):
        if self.local:
            return await asyncio.to_thread(self.local.find, collection, query, limit, projection)
        return [public(d) async for d in self.db[collection].find(query or {}, projection).limit(limit)]

    async def close(self):
        if self.client:
            await self.client.close()


def make_store(settings: Settings):
    return LocalStore(settings.data_dir) if settings.mode == "local" else MongoStore(settings)


def create_indexes(store: MongoStore, settings: Settings, search=True):
    for name in SCOPED:
        store.db[name].create_index([("analysisId", 1), ("id", 1)], unique=True)
    for name in ("repositories", "analyses", "jobs", "embedding_cache", "users",
                 "user_analyses", "auth_sessions", "guest_sessions", "oauth_states", "demo_answers", "chat_turns", "investigations"):
        store.db[name].create_index("id", unique=True)
    store.db.user_analyses.create_index([("userId", 1), ("createdAt", -1)])
    store.db.chat_turns.create_index([("userId", 1), ("analysisId", 1), ("createdAt", -1)])
    store.db.investigations.create_index([("userId", 1), ("analysisId", 1), ("createdAt", -1)])
    store.db.oauth_states.create_index("deleteAfter", expireAfterSeconds=0)
    store.db.auth_sessions.create_index("deleteAfter", expireAfterSeconds=0)
    store.db.guest_sessions.create_index("deleteAfter", expireAfterSeconds=0)
    store.db.repositories.create_index("githubId", unique=True, sparse=True)
    store.db.analyses.create_index([("repositoryId", 1), ("commitSha", 1), ("profile", 1)], unique=True)
    store.db.files.create_index([("analysisId", 1), ("path", 1)], unique=True)
    for endpoint in ("source", "target"):
        store.db.edges.create_index([("analysisId", 1), (endpoint, 1), ("kind", 1)])
    store.db.nodes.create_index([("analysisId", 1), ("fileId", 1)])
    store.db.chunks.create_index([("analysisId", 1), ("fileId", 1)])
    store.db.jobs.create_index([("status", 1), ("heartbeatAt", 1)])
    if search:
        existing = {index["name"] for index in store.db.chunks.list_search_indexes()}
        if settings.effective_vector_index not in existing:
            store.db.chunks.create_search_index(SearchIndexModel(name=settings.effective_vector_index, type="vectorSearch", definition={"fields": [
                {"type": "vector", "path": settings.effective_embedding_field, "numDimensions": settings.effective_embedding_dimensions, "similarity": "cosine"},
                *[{"type": "filter", "path": field} for field in ("analysisId", "repositoryId", "embeddingConfig", "language")],
            ]}))
        if settings.search_index not in existing:
            store.db.chunks.create_search_index(SearchIndexModel(name=settings.search_index, definition={"mappings": {"dynamic": False, "fields": {
                "text": {"type": "string"}, "path": {"type": "string"}, "symbol": {"type": "string"},
                "analysisId": {"type": "token"}, "embeddingConfig": {"type": "token"},
            }}}))


if __name__ == "__main__":
    from .config import get_settings
    settings = get_settings()
    store = MongoStore(settings)
    create_indexes(store, settings)
    print("MongoDB collections and Atlas search indexes requested.")
    store.close()
