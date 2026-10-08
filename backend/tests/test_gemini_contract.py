import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from navigator import rag
from navigator.config import Settings
from navigator.contracts import ChatRequest, DomainError
from navigator.ingestion import make_file
from navigator.storage import LocalStore


def test_embedding_batch_is_separate_content_and_normalized(monkeypatch):
    config = Settings(_env_file=None, embedding_dimensions=3)
    generate = Mock(return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[3., 0., 4.]), SimpleNamespace(values=[0., 2., 0.])]))
    client = SimpleNamespace(models=SimpleNamespace(embed_content=generate), close=Mock())
    monkeypatch.setattr(rag, "ai_client", lambda _: client)
    vectors = rag.embed_batch(["function first(){}", "function second(){}"], config)
    assert vectors == [[0.6, 0., 0.8], [0., 1., 0.]]
    contents = generate.call_args.kwargs["contents"]
    assert len(contents) == 2
    assert "first" in contents[0].parts[0].text and "second" in contents[1].parts[0].text
    client.close.assert_called_once()


@pytest.mark.parametrize("citation_id, succeeds", [("S1", True), ("invented", False)])
def test_answer_uses_json_schema_and_enforces_source_registry(monkeypatch, tmp_path, citation_id, succeeds):
    config = Settings(_env_file=None)
    store = LocalStore(tmp_path)
    file = make_file("src/example.ts", b"export const example = 1;\n", "a")
    store.put("files", file)
    chunk = {"id": "chunk1", "analysisId": "a", "fileId": file["id"], "path": file["path"], "startLine": 1, "endLine": 1, "text": file["content"]}
    monkeypatch.setattr(rag, "retrieve", lambda *_: ([chunk], [], [file["id"]]))
    content = json.dumps({"blocks": [{"text": "The module exports a constant.", "kind": "fact", "citationIds": [citation_id], "edgeIds": []}]})
    generate = Mock(return_value=SimpleNamespace(text=content))
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate), close=Mock())
    monkeypatch.setattr(rag, "ai_client", lambda _: client)
    if succeeds:
        answer = rag.answer_question(store, {"id": "a", "commitSha": "abc"}, ChatRequest(message="Explain this module"), config)
        assert answer["citations"][0]["fileId"] == file["id"]
        assert answer["citations"][0]["commitSha"] == "abc"
        assert generate.call_args.kwargs["config"].response_json_schema is not None
        assert generate.call_args.kwargs["config"].response_schema is None
    else:
        with pytest.raises(DomainError) as error:
            rag.answer_question(store, {"id": "a", "commitSha": "abc"}, ChatRequest(message="Explain this module"), config)
        assert error.value.code == "GROUNDING_FAILED"
        assert generate.call_count == 2
    client.close.assert_called_once()
