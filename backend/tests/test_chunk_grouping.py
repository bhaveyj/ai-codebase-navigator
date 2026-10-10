from navigator.analysis import build_chunks
from navigator.config import Settings
from navigator.ingestion import make_file


def test_adjacent_imports_share_one_citable_chunk_without_losing_source():
    imports = "".join(f"import item{i} from './item{i}';\n" for i in range(18))
    source = imports + "\nexport function main() {\n  return item0;\n}\n"
    file = make_file("src/main.ts", source.encode(), "analysis")
    regions = [{"fileId": file["id"], "kind": "statement", "startLine": i, "endLine": i} for i in range(1, 19)]
    regions.append({"fileId": file["id"], "kind": "function", "symbolId": "main", "signature": "main()", "startLine": 20, "endLine": 22})
    settings = Settings(_env_file=None)

    chunks = build_chunks([file], regions, [{"id": "main", "name": "main"}], settings)

    assert len(chunks) < 6
    grouped = next(chunk for chunk in chunks if chunk["kind"] == "imports")
    assert (grouped["startLine"], grouped["endLine"]) == (1, 18)
    assert grouped["text"] == imports
    symbol = next(chunk for chunk in chunks if chunk.get("symbolId") == "main")
    assert (symbol["startLine"], symbol["endLine"]) == (20, 22)
    assert all(chunk["text"] == "".join(source.splitlines(keepends=True)[chunk["startLine"] - 1:chunk["endLine"]]) for chunk in chunks)


def test_legacy_chunk_profile_keeps_existing_chunk_boundaries():
    source = "import first from './first';\nimport second from './second';\n"
    file = make_file("src/old.ts", source.encode(), "analysis")
    regions = [{"fileId": file["id"], "kind": "statement", "startLine": i, "endLine": i} for i in (1, 2)]
    settings = Settings(_env_file=None, chunk_profile_version=1)

    chunks = build_chunks([file], regions, [], settings)

    assert len(chunks) == 2
    assert settings.analysis_profile.startswith("typescript-v1:chunk-v1:")


def test_grouped_chunks_never_cross_file_boundaries_or_size_limit():
    files = [make_file(f"src/{name}.ts", ("import x from './x';\n" * 120).encode(), "analysis") for name in ("a", "b")]
    regions = [{"fileId": file["id"], "kind": "statement", "startLine": i, "endLine": i}
               for file in files for i in range(1, 121)]

    chunks = build_chunks(files, regions, [], Settings(_env_file=None))

    assert len(chunks) < 20
    assert {chunk["fileId"] for chunk in chunks} == {file["id"] for file in files}
    assert all(len(chunk["text"]) <= 1800 for chunk in chunks)
    for file in files:
        assert "".join(chunk["text"] for chunk in chunks if chunk["fileId"] == file["id"]) == file["content"]
