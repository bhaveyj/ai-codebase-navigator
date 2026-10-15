import json

import pytest

from navigator import rag
from navigator.config import Settings
from navigator.contracts import ChatRequest, DomainError
from navigator.ingestion import make_file
from navigator.storage import LocalStore


def cloudflare_settings(**changes):
    return Settings(
        _env_file=None,
        CLOUDFLARE_ACCOUNT_ID="account",
        CLOUDFLARE_API_TOKEN="token",
        GROQ_API_KEY="groq-key",
        **changes,
    )


def test_auto_selection_and_legacy_analysis_profile_keep_separate_vector_spaces():
    current = cloudflare_settings()
    assert current.effective_embedding_provider == "cloudflare"
    assert current.effective_answer_provider == "groq"
    assert current.analysis_profile.startswith("multilang-v1:chunk-v2:@cf/qwen/")
    assert current.effective_embedding_dimensions == 1024
    new_vector = rag.vector_pipeline("new", [0.0] * 1024, current)[0]["$vectorSearch"]
    assert new_vector["index"] == "chunks_vector_v2" and new_vector["path"] == "cloudflareEmbedding"

    old = current.for_analysis_profile("typescript-v1:chunk-v1:gemini-embedding-2:768:code-retrieval-v1")
    assert old.effective_embedding_provider == "gemini"
    assert old.effective_answer_provider == "groq"
    assert old.chunk_profile_version == 1
    assert old.embedding_config == "gemini-embedding-2:768:code-retrieval-v1"
    old_vector = rag.vector_pipeline("old", [0.0] * 768, old)[0]["$vectorSearch"]
    assert old_vector["index"] == "chunks_vector_v1" and old_vector["path"] == "embedding"

    changed = current.model_copy(update={"cloudflare_embedding_model": "@cf/qwen/future-model"})
    recovered = changed.for_analysis_profile(current.analysis_profile)
    assert recovered.cloudflare_embedding_model == current.cloudflare_embedding_model
    assert recovered.embedding_config == current.embedding_config


def test_cloudflare_embedding_dispatch_does_not_create_gemini_client(monkeypatch):
    settings = cloudflare_settings()
    calls = []
    monkeypatch.setattr(rag, "ai_client", lambda *_: pytest.fail("Gemini client was used"))
    monkeypatch.setattr(rag, "embed_cloudflare", lambda texts, _settings, query=False: calls.append((texts, query)) or [[1.0]])

    assert rag.embed_batch(["source"], settings) == [[1.0]]
    assert rag.embed_batch(["question"], settings, query=True) == [[1.0]]
    assert calls == [(["source"], False), (["question"], True)]


def test_cloudflare_chunks_save_and_recover_vectors_in_separate_field(monkeypatch, tmp_path):
    settings = cloudflare_settings()
    store = LocalStore(tmp_path)
    chunk = {"id": "chunk-a", "analysisId": "analysis", "inputHash": "hash-a", "embeddingText": "source"}
    calls = []
    monkeypatch.setattr(rag, "embed_batch", lambda texts, _settings: calls.append(texts) or [[1.0, 0.0]])

    rag.embed_chunks(store, [chunk], settings, lambda *_: None, lambda: None)

    saved = store.one("chunks", {"id": "chunk-a"})
    assert saved["cloudflareEmbedding"] == [1.0, 0.0]
    assert "embedding" not in saved
    assert calls == [["source"]]

    resumed = {key: value for key, value in chunk.items() if key != "cloudflareEmbedding"}
    monkeypatch.setattr(rag, "embed_batch", lambda *_args: pytest.fail("cached vector was not reused"))
    rag.embed_chunks(store, [resumed], settings, lambda *_: None, lambda: None)
    assert resumed["cloudflareEmbedding"] == [1.0, 0.0]


def test_groq_answer_receives_schema_and_retains_citation_validation(monkeypatch, tmp_path):
    settings = cloudflare_settings()
    store = LocalStore(tmp_path)
    file = make_file("src/app.ts", b"export const answer = 42;\n", "analysis")
    store.put("files", file)
    evidence = {"id": "chunk:app", "analysisId": "analysis", "fileId": file["id"], "path": file["path"],
                "startLine": 1, "endLine": 1, "text": file["content"]}
    monkeypatch.setattr(rag, "retrieve", lambda *_args: ([evidence], [], []))
    monkeypatch.setattr(rag, "ai_client", lambda *_: pytest.fail("Gemini client was used"))
    requests = []

    def generate(system, payload, _settings):
        requests.append((system, payload))
        return {"blocks": [{"text": "The answer is exported.", "kind": "fact", "citationIds": ["S1"], "edgeIds": []}]}

    monkeypatch.setattr(rag, "generate_groq", generate)
    question = ChatRequest(message="What is exported?", history=[
        {"role": "user", "content": "old turn"},
        {"role": "assistant", "content": "a" * 1500},
        {"role": "user", "content": "b" * 1500},
    ])
    answer = rag.answer_question(store, {"id": "analysis", "commitSha": "abc"}, question, settings)

    assert "citationIds" in requests[0][0] and "edgeIds" in requests[0][0]
    assert "3 to 6 focused" in requests[0][0]
    assert requests[0][1]["sources"][0]["citationId"] == "S1"
    assert [len(turn["content"]) for turn in requests[0][1]["recentConversation"]] == [1000, 1000]
    assert answer["citations"][0]["chunkId"] == evidence["id"]
    assert answer["blocks"][0]["citationIds"] == ["S1"]

    invalid_calls = []
    def unsupported(*_args):
        invalid_calls.append(1)
        return {"blocks": [{"text": "Fabricated", "kind": "fact", "citationIds": ["S999"], "edgeIds": []}]}
    monkeypatch.setattr(rag, "generate_groq", unsupported)
    with pytest.raises(DomainError, match="citation validation") as caught:
        rag.answer_question(store, {"id": "analysis", "commitSha": "abc"}, ChatRequest(message="What is exported?"), settings)
    assert caught.value.code == "GROUNDING_FAILED"
    assert len(invalid_calls) == 1


def test_detail_follow_up_keeps_previous_topic_in_generation(monkeypatch, tmp_path):
    settings = cloudflare_settings()
    store = LocalStore(tmp_path)
    file = make_file("src/auth.py", b"def authenticate(token):\n    return verify(token)\n", "analysis")
    store.put("files", file)
    evidence = {"id": "chunk:auth", "analysisId": "analysis", "fileId": file["id"],
                "path": file["path"], "startLine": 1, "endLine": 2, "text": file["content"]}
    monkeypatch.setattr(rag, "retrieve", lambda *_args: ([evidence], [], []))
    captured = []
    def generate(_system, payload, _settings):
        captured.append(payload)
        return {"blocks": [{"text": "The function verifies a token.", "kind": "fact", "citationIds": ["S1"], "edgeIds": []}]}
    monkeypatch.setattr(rag, "generate_groq", generate)
    request = ChatRequest(message="explain in detail", history=[
        {"role": "user", "content": "How does authentication work?"},
        {"role": "assistant", "content": "Authentication uses a token."},
    ])

    answer = rag.answer_question(store, {"id": "analysis", "commitSha": "abc"}, request, settings)

    assert captured[0]["question"] == "explain in detail"
    assert captured[0]["conversationTopic"] == "How does authentication work?"
    assert answer["blocks"][0]["citationIds"] == ["S1"]


def test_stream_answer_emits_validated_blocks_before_completion(monkeypatch, tmp_path):
    settings = cloudflare_settings()
    store = LocalStore(tmp_path)
    file = make_file("src/app.py", b"ANSWER = 42\n", "analysis")
    store.put("files", file)
    evidence = {"id": "chunk:app", "analysisId": "analysis", "fileId": file["id"], "path": file["path"],
                "startLine": 1, "endLine": 1, "text": file["content"]}
    monkeypatch.setattr(rag, "retrieve", lambda *_args: ([evidence], [], []))
    text = json.dumps({"blocks": [
        {"text": 'The value is {42}.', "kind": "fact", "citationIds": ["S1"], "edgeIds": []},
        {"text": "Other behavior is unknown.", "kind": "unknown", "citationIds": [], "edgeIds": []},
    ]})
    monkeypatch.setattr(rag, "stream_groq", lambda *_args: iter([text[:27], text[27:88], text[88:120], text[120:]]))
    events = list(rag.stream_answer_question(store, {"id": "analysis", "commitSha": "abc"}, ChatRequest(message="What is exported?"), settings))
    types = [event["type"] for event in events]
    assert types[:2] == ["status", "status"]
    assert "draft" in types and types.index("draft") < types.index("block")
    assert "".join(event["delta"] for event in events[2:types.index("block")] if event["type"] == "draft") == "The value is {42}."
    blocks = [event for event in events if event["type"] == "block"]
    assert len(blocks) == 2 and types[-1] == "done"
    assert blocks[0]["citations"][0]["path"] == "src/app.py"
    assert events[-1]["answer"]["blocks"] == [block["block"] for block in blocks]


def test_stream_answer_rejects_unsupported_citation(monkeypatch, tmp_path):
    settings = cloudflare_settings()
    store = LocalStore(tmp_path)
    file = make_file("src/app.py", b"ANSWER = 42\n", "analysis")
    store.put("files", file)
    evidence = {"id": "chunk:app", "analysisId": "analysis", "fileId": file["id"], "path": file["path"],
                "startLine": 1, "endLine": 1, "text": file["content"]}
    monkeypatch.setattr(rag, "retrieve", lambda *_args: ([evidence], [], []))
    monkeypatch.setattr(rag, "stream_groq", lambda *_args: iter(['{"blocks":[{"text":"Invented","kind":"fact","citationIds":["S99"],"edgeIds":[]}]}']))
    with pytest.raises(DomainError, match="citation validation"):
        list(rag.stream_answer_question(store, {"id": "analysis", "commitSha": "abc"}, ChatRequest(message="What is exported?"), settings))


def test_stream_answer_preserves_only_validated_blocks_if_later_block_fails(monkeypatch, tmp_path):
    settings = cloudflare_settings()
    store = LocalStore(tmp_path)
    file = make_file("src/app.py", b"ANSWER = 42\n", "analysis")
    store.put("files", file)
    evidence = {"id": "chunk:app", "analysisId": "analysis", "fileId": file["id"], "path": file["path"],
                "startLine": 1, "endLine": 1, "text": file["content"]}
    monkeypatch.setattr(rag, "retrieve", lambda *_args: ([evidence], [], []))
    first = '{"blocks":[{"text":"The value is exported.","kind":"fact","citationIds":["S1"],"edgeIds":[]}'
    second = ',{"text":"Unsupported claim.","kind":"fact","citationIds":["S99"],"edgeIds":[]}]}'
    monkeypatch.setattr(rag, "stream_groq", lambda *_args: iter([first, second]))
    events = list(rag.stream_answer_question(store, {"id": "analysis", "commitSha": "abc"}, ChatRequest(message="What is exported?"), settings))
    assert [event["type"] for event in events][-2:] == ["block", "done"]
    assert len(events[-1]["answer"]["blocks"]) == 1
    assert "incomplete" in events[-1]["answer"]["warnings"][0]


def test_groq_bounds_combined_source_graph_and_history_payload():
    sources = [{"citationId": f"S{index}", "path": f"src/module-{index}.ts", "lines": [1, 25],
                "code": "export const value = 42;\n" * 27} for index in range(1, 17)]
    edges = [{"id": f"edge-{index}", "source": f"file:src/module-{index}.ts",
              "target": f"file:src/module-{index + 1}.ts", "kind": "imports",
              "specifier": f"./module-{index + 1}", "resolution": "resolved",
              "unusedMetadata": "x" * 200} for index in range(39)]
    payload = {"question": "How is this project structured?", "selectedNodeId": None,
               "recentConversation": [{"role": "user", "content": "x" * 1000},
                                      {"role": "assistant", "content": "y" * 1000}],
               "sources": sources, "staticRelationships": edges}
    registry = {source["citationId"]: source for source in sources}

    bounded_registry = rag.bound_groq_payload(payload, registry)

    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 15000
    assert len(json.dumps(payload["staticRelationships"], ensure_ascii=False, separators=(",", ":"))) <= 3500
    assert payload["sources"][0]["citationId"] == "S1"
    assert set(bounded_registry) == {source["citationId"] for source in payload["sources"]}
    assert all("unusedMetadata" not in edge and "resolution" not in edge for edge in payload["staticRelationships"])


def test_groq_rejects_unavoidably_oversized_payload_before_provider_call():
    payload = {"question": "q" * 4000, "selectedNodeId": "file:" + "p" * 2000,
               "recentConversation": [], "staticRelationships": [],
               "sources": [{"citationId": "S1", "path": "src/large.js", "lines": [1, 1], "code": "x" * 10000}]}
    with pytest.raises(DomainError) as caught:
        rag.bound_groq_payload(payload, {"S1": {"id": "S1"}})
    assert caught.value.code == "GROQ_EVIDENCE_TOO_LARGE"
    assert not caught.value.retryable
