"""Evidence should point to implementation regions for component questions."""

from types import SimpleNamespace

from navigator.config import Settings
from navigator.contracts import ChatRequest
from navigator import rag
from navigator.rag import retrieve


def test_follow_up_uses_previous_substantive_question_for_retrieval(monkeypatch):
    questions = []
    class Store:
        is_mongo = True
        def __init__(self):
            self.db = SimpleNamespace(chunks=SimpleNamespace(aggregate=self.aggregate))
        def aggregate(self, pipeline):
            return []
        def one(self, *_args):
            return None
        def put(self, *_args):
            pass
        def find(self, collection, *_args, **_kwargs):
            return []
    monkeypatch.setattr(rag, "embed", lambda question, *_args, **_kwargs: questions.append(("vector", question)) or [0.1])
    monkeypatch.setattr(rag, "lexical_pipeline", lambda _id, question, _settings: questions.append(("lexical", question)) or [{}])
    request = ChatRequest(message="explain in detail", history=[
        {"role": "user", "content": "How does authentication work?"},
        {"role": "assistant", "content": "It uses tokens."},
        {"role": "user", "content": "tell me more"},
        {"role": "assistant", "content": "A short answer."},
    ])

    retrieve(Store(), {"id": "analysis", "ragReady": True}, request, Settings(_env_file=None))

    assert questions == [("vector", "How does authentication work?"),
                         ("lexical", "How does authentication work?")]
    assert rag.conversation_topic(ChatRequest(message="Explain authentication in detail", history=request.history)) is None


def test_gateway_overview_prioritizes_routes_and_security_over_manifest_and_selection_noise():
    settings = Settings(_env_file=None, CLOUDFLARE_ACCOUNT_ID="account", CLOUDFLARE_API_TOKEN="token", GROQ_API_KEY="key")
    gateway = "apigateway/src/main/java/example/GatewayConfig.java"
    filter_path = "apigateway/src/main/java/example/AuthGlobalFilter.java"
    pom = "apigateway/pom.xml"
    other = "BookService/src/main/java/example/BookServiceFilter.java"
    paths = [gateway, filter_path, pom, other]
    nodes = [{"id": "file:" + path, "kind": "file", "name": path.rsplit("/", 1)[-1], "path": path} for path in paths]

    def chunk(id, path, start, end, text, kind="method", symbol=""):
        return {"id": id, "analysisId": "a", "fileId": "file:" + path, "path": path,
                "startLine": start, "endLine": end, "text": text, "kind": kind, "symbol": symbol}

    route = chunk("route", gateway, 34, 48, "@Bean RouteLocator routes() { return builder.routes().route(\"books\", r -> r.path(\"/books/**\").uri(\"lb://bookservice\")); }", symbol="routes")
    security = chunk("security", gateway, 17, 32, "@Bean SecurityWebFilterChain springSecurityFilterChain() { authorizeExchange(); return http.build(); }", kind="declarations")
    tiny = chunk("tiny", gateway, 55, 55, "}", kind="top-level")
    auth = chunk("auth", filter_path, 38, 98, "class AuthGlobalFilter { Mono<Void> filter() { authenticate(); return chain.filter(exchange); } }", symbol="filter")
    auth_tiny = chunk("auth-tiny", filter_path, 112, 112, "}", kind="top-level")
    manifest = chunk("manifest", pom, 1, 91, "<dependency>spring-cloud-starter-gateway</dependency>", kind="top-level")
    unrelated = chunk("unrelated", other, 27, 70, "class BookServiceFilter { authenticate(); }", symbol="doFilterInternal")
    all_chunks = [route, security, tiny, auth, auth_tiny, manifest, unrelated]
    vector = [tiny, auth_tiny, manifest, auth, route]
    lexical = [unrelated, manifest, auth]

    class Store:
        is_mongo = True
        def __init__(self):
            self.db = SimpleNamespace(chunks=SimpleNamespace(aggregate=self.aggregate))
        def aggregate(self, pipeline):
            return vector if "$vectorSearch" in pipeline[0] else lexical
        def one(self, collection, _query):
            assert collection == "embedding_cache"
            return {"embedding": [0.1], "embeddingConfig": settings.embedding_config}
        def find(self, collection, query, limit=None, projection=None):
            if collection == "nodes": return nodes
            if collection == "edges": return []
            if collection == "chunks":
                ids = set(query["fileId"]["$in"])
                return [item for item in all_chunks if item["fileId"] in ids][:limit]
            raise AssertionError(collection)

    evidence, _, _ = retrieve(Store(), {"id": "a", "ragReady": True}, ChatRequest(
        message="explain the api gateway", selectedNodeId="file:" + filter_path), settings)
    ids = [item["id"] for item in evidence]
    assert ids[0] == "route"
    assert "security" in ids and "auth" in ids
    assert "manifest" not in ids and "tiny" not in ids and "auth-tiny" not in ids
    assert "unrelated" not in ids

    build_evidence, _, _ = retrieve(Store(), {"id": "a", "ragReady": True}, ChatRequest(
        message="Which dependencies does the API gateway POM use?"), settings)
    assert "manifest" in [item["id"] for item in build_evidence]
