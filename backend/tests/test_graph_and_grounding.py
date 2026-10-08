import pytest

from navigator.analysis import build_chunks
from navigator.config import Settings
from navigator.contracts import DomainError
from navigator.graph import neighbors, project_graph
from navigator.ingestion import make_file
from navigator.rag import dependency_answer, embed_chunks, lexical_pipeline, validate_answer, vector_pipeline
from navigator.storage import LocalStore


def sample():
    nodes = [{"id": "a", "kind": "file", "name": "a.ts"}, {"id": "b", "kind": "file", "name": "b.ts"}, {"id": "c", "kind": "file", "name": "c.ts"}]
    edges = [{"id": "ab", "source": "a", "target": "b", "kind": "imports", "fileId": "a", "startLine": 1, "endLine": 1}, {"id": "bc", "source": "b", "target": "c", "kind": "imports"}, {"id": "ca", "source": "c", "target": "a", "kind": "contains"}]
    return nodes, edges


def test_direction_and_depth_follow_source_to_dependency():
    nodes, edges = sample()
    assert {n["id"] for n in neighbors(nodes, edges, "b", "dependencies")[0]} == {"b", "c"}
    assert {n["id"] for n in neighbors(nodes, edges, "b", "dependents")[0]} == {"a", "b"}
    assert {n["id"] for n in neighbors(nodes, edges, "a", depth=2)[0]} == {"a", "b", "c"}
    with pytest.raises(DomainError):
        neighbors(nodes, edges, "different-analysis-node")


def test_graph_bounds_have_no_dangling_edges():
    nodes = [{"id": str(n), "kind": "file", "name": f"{n:03d}.ts"} for n in range(150)]
    edges = [{"id": f"e{n}", "source": "0", "target": str(n), "kind": "imports"} for n in range(1, 150)]
    graph = project_graph(nodes, edges, "files")
    ids = {n["id"] for n in graph["nodes"]}
    assert len(ids) == 100 and graph["omittedNodes"] == 50
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])


def test_root_module_drills_into_repository_files():
    nodes = [{"id": "module:.", "kind": "module", "name": "Root", "path": "."}, {"id": "file:index.ts", "kind": "file", "name": "index.ts", "path": "index.ts", "parentId": "module:."}]
    graph = project_graph(nodes, [], "files", "module:.")
    assert [node["id"] for node in graph["nodes"]] == ["file:index.ts"]


def test_default_modules_hide_deep_folders_but_preserve_dependency_projection():
    nodes = [
        {"id": "module:src/a", "kind": "module", "name": "a", "path": "src/a"},
        {"id": "module:src/a/deep", "kind": "module", "name": "deep", "path": "src/a/deep", "parentId": "module:src/a"},
        {"id": "module:src/b", "kind": "module", "name": "b", "path": "src/b"},
        {"id": "file:a", "kind": "file", "name": "a.ts", "parentId": "module:src/a/deep"},
        {"id": "file:b", "kind": "file", "name": "b.ts", "parentId": "module:src/b"},
    ]
    edges = [{"id": "import", "source": "file:a", "target": "file:b", "kind": "imports"}]
    graph = project_graph(nodes, edges)
    assert "module:src/a/deep" not in {node["id"] for node in graph["nodes"]}
    assert graph["edges"][0]["source"] == "module:src/a"
    assert graph["edges"][0]["edgeIds"] == ["import"]


@pytest.mark.parametrize("block", [
    {"text": "Unsupported source", "kind": "fact", "citationIds": ["S999"]},
    {"text": "Uncited fact", "kind": "fact", "citationIds": []},
    {"text": "Invented relationship", "kind": "fact", "citationIds": ["S1"], "edgeIds": ["not-in-graph"]},
])
def test_generation_rejects_invented_citations_and_edges(block):
    with pytest.raises(ValueError):
        validate_answer({"blocks": [block]}, {"S1": {}}, {"edge1"})


def test_generation_accepts_supported_and_unknown_claims():
    blocks = validate_answer({"blocks": [{"text": "Function exists", "kind": "fact", "citationIds": ["S1"], "edgeIds": ["edge1"]}, {"text": "Not established", "kind": "unknown", "citationIds": []}]}, {"S1": {}}, {"edge1"})
    assert len(blocks) == 2


def test_queries_always_filter_analysis_and_embedding_profile():
    config = Settings(_env_file=None)
    query = vector_pipeline("immutable-a", [0.1] * 768, config)[0]["$vectorSearch"]
    assert query["filter"] == {"analysisId": "immutable-a", "embeddingConfig": config.embedding_config}
    filters = lexical_pipeline("immutable-a", "question", config)[0]["$search"]["compound"]["filter"]
    assert filters[0]["equals"]["value"] == "immutable-a"
    assert filters[1]["equals"]["value"] == config.embedding_config


def test_semantic_chunks_preserve_source_and_metadata():
    file = make_file("src/a.ts", b"import x from './x';\nexport function f() {\n return x;\n}\n", "a")
    region = {"fileId": file["id"], "symbolId": "f", "kind": "function", "startLine": 2, "endLine": 4, "signature": "f()"}
    chunks = build_chunks([file], [region], [{"id": "f", "name": "f"}], Settings(_env_file=None))
    assert len(chunks) == 2
    symbol = next(c for c in chunks if c.get("symbolId"))
    assert (symbol["symbol"], symbol["startLine"], symbol["endLine"]) == ("f", 2, 4)
    assert symbol["text"] == "".join(file["content"].splitlines(keepends=True)[1:4])


def test_long_lines_do_not_discard_preceding_or_following_source():
    content = "before\n" + "x" * 9000 + "\nafter\n"
    file = make_file("a.ts", content.encode(), "a")
    chunks = build_chunks([file], [], [], Settings(_env_file=None))
    assert "".join(c["text"] for c in chunks) == content
    assert all(1 <= c["startLine"] <= c["endLine"] <= file["lineCount"] for c in chunks)


def test_embedding_failure_preserves_completed_batches(monkeypatch, tmp_path):
    from navigator import rag
    config = Settings(_env_file=None, embedding_batch_size=1, embedding_batch_retries=0, embedding_inputs_per_minute=0)
    store = LocalStore(tmp_path)
    chunks = [{"id": f"c{n}", "analysisId": "a", "inputHash": f"h{n}", "embeddingText": str(n)} for n in range(3)]
    def fake_embed(texts, *_args):
        if texts == ["2"]:
            raise DomainError("GEMINI_RATE_LIMITED", "test", 503)
        return [[0.1] * 768]
    monkeypatch.setattr(rag, "embed_batch", fake_embed)
    with pytest.raises(DomainError):
        embed_chunks(store, chunks, config, lambda *_: None, lambda: None)
    assert len(store.find("embedding_cache")) == 2
    assert len(store.find("chunks")) == 2


def test_dependency_answers_cite_actual_import_lines():
    nodes, edges = sample()
    result = dependency_answer({"id": "analysis-a", "commitSha": "abc"}, nodes, edges, [{"id": "a", "path": "src/a.ts", "lineCount": 10}], "b", "dependents")
    assert result["mode"] == "graph"
    assert result["citations"][0]["analysisId"] == "analysis-a"
    assert result["citations"][0]["startLine"] == 1
