import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from navigator import auth
from navigator.api import main
from navigator.config import Settings
from navigator.exploration import architecture_tour, compare_snapshots, impact_analysis, source_diff
from navigator.graph import project_graph
from navigator.ingestion import github_metadata
from navigator.storage import AsyncStore, LocalStore


def fixture_graph(analysis_id="a"):
    paths = ["src/main.ts", "src/service.ts", "src/db.ts", "tests/service.test.ts"]
    files = [{"id": f"file:{path}", "analysisId": analysis_id, "path": path,
              "language": "typescript", "content": f"// {path}\n", "lineCount": 2,
              "contentHash": hashlib.sha256(path.encode()).hexdigest()} for path in paths]
    nodes = [{"id": file["id"], "analysisId": analysis_id, "kind": "file", "name": file["path"].split('/')[-1], "path": file["path"]} for file in files]
    nodes.append({"id": "fn", "analysisId": analysis_id, "kind": "symbol", "name": "run", "path": "src/service.ts", "fileId": "file:src/service.ts", "symbolKind": "function", "startLine": 1, "endLine": 2})
    edges = [{"id": str(index), "analysisId": analysis_id, "source": f"file:{source}", "target": f"file:{target}", "fileId": f"file:{source}", "startLine": 1, "endLine": 1, "kind": "imports", "resolution": "resolved"}
             for index, (source, target) in enumerate([(paths[0],paths[1]), (paths[1],paths[2]), (paths[3],paths[1]), (paths[2],paths[0])])]
    analysis = {"id": analysis_id, "repositoryId": "repo", "repositoryName": "example/repo", "commitSha": "a"*40,
                "status": "ready", "phase": "ready", "graphReady": True, "ragReady": False, "jobId": "job",
                "counts": {"files": 4,"symbols": 1,"edges": 4,"chunks": 4}, "languages": ["typescript"],
                "createdAt": "2026-10-15T00:00:00Z", "warnings": [], "overview": {"frameworks": [], "integrations": [],
                "entryPoints": [paths[0]], "routes": [{"method": "GET", "route": "/run", "filePath": paths[0]}]}}
    return analysis, nodes, edges, files


def test_impact_follows_reverse_dependencies_and_handles_cycles():
    analysis, nodes, edges, files = fixture_graph()
    result = impact_analysis(analysis, nodes, edges, files, "file:src/service.ts", 3)
    assert {item["node"]["path"] for item in result["direct"]} == {"src/main.ts", "tests/service.test.ts"}
    assert [item["node"]["path"] for item in result["transitive"]] == ["src/db.ts"]
    assert result["tests"][0]["node"]["path"] == "tests/service.test.ts"
    assert result["routes"][0]["route"] == "/run"
    assert result["direct"][0]["citation"]["analysisId"] == "a"
    assert len(impact_analysis(analysis,nodes,edges,files,"file:src/service.ts",1)["transitive"]) == 0
    assert "file scope" in impact_analysis(analysis,nodes,edges,files,"fn")["warnings"][1]


def test_comparison_ignores_moving_lines_and_analysis_specific_ids():
    analysis,nodes,edges,files = fixture_graph()
    newer = [{**node, "startLine": 20, "id": "new-fn" if node["id"] == "fn" else node["id"]} for node in nodes]
    changed = [{**file, "contentHash": "changed"} if file["path"] == "src/service.ts" else file for file in files]
    result = compare_snapshots(analysis,analysis,files,changed,nodes,newer,edges,edges)
    assert [file["path"] for file in result["files"]] == ["src/service.ts"]
    assert result["symbolsAdded"] == result["symbolsRemoved"] == 0
    assert result["relationshipsAdded"] == result["relationshipsRemoved"] == []
    result = compare_snapshots(analysis,analysis,files,changed[:-1],nodes,newer,edges,edges[:-1])
    assert result["files"][-1]["status"] == "removed"
    assert len(result["relationshipsRemoved"]) == 1


def test_diff_uses_redacted_snapshot_content_and_is_bounded():
    result = source_diff({"content":"old\n","redacted":True},{"content":"new\n"},"src/a.ts",limit=4)
    assert result["redacted"] and result["omittedLines"] > 0
    assert len(result["lines"]) == 4
    assert "-old" in result["lines"]


def test_tour_steps_are_actual_dependencies_and_stop_at_cycles():
    analysis,nodes,edges,files = fixture_graph()
    result = architecture_tour(analysis,nodes,edges,files)
    assert [step["node"]["path"] for step in result["steps"]] == ["src/main.ts", "src/service.ts", "src/db.ts"]
    assert result["steps"][1]["citation"]["path"] == "src/main.ts"
    assert result["steps"][1]["relationship"] == "imports"
    assert architecture_tour(analysis,nodes,edges,files,"fn")["steps"][0]["node"]["path"] == "src/service.ts"


def test_graph_expansion_has_no_dangling_edges():
    nodes = [{"id":str(n),"kind":"file","name":str(n)} for n in range(260)]
    edges = [{"id":str(n),"source":"0","target":str(n),"kind":"imports"} for n in range(1,260)]
    result = project_graph(nodes,edges,"files",limit=200)
    assert len(result["nodes"]) == 200 and result["omittedNodes"] == 60
    ids = {node["id"] for node in result["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in result["edges"])


def test_module_overview_keeps_root_source_relationships():
    nodes = [{"id":"module:.","kind":"module","name":"Root","path":"."},
             {"id":"module:src","kind":"module","name":"src","path":"src"},
             {"id":"file:main.ts","kind":"file","name":"main.ts","path":"main.ts","language":"typescript","parentId":"module:."},
             {"id":"file:src/run.ts","kind":"file","name":"run.ts","path":"src/run.ts","parentId":"module:src"}]
    edges = [{"id":"import","source":"file:main.ts","target":"file:src/run.ts","kind":"imports"}]
    result = project_graph(nodes,edges)
    assert {node["id"] for node in result["nodes"]} == {"module:.","module:src"}
    assert result["edges"][0]["source"] == "module:."


@pytest.fixture
def secured(tmp_path,monkeypatch):
    config = Settings(_env_file=None,NAVIGATOR_MODE="production",MONGODB_URI="mongodb://unused")
    store = LocalStore(tmp_path)
    monkeypatch.setattr(main,"make_store",lambda _:store)
    monkeypatch.setattr(main,"AsyncStore",lambda *_:AsyncStore(config,local=store))
    monkeypatch.setattr(main,"create_indexes",lambda *_:None)
    monkeypatch.setattr(main,"recover_jobs",lambda *_:None)
    monkeypatch.setattr(main,"QuotaGate",lambda *_:type("Gate",(),{"close":lambda self:None})())
    for id in ("a","b","private"):
        analysis,nodes,edges,files = fixture_graph(id)
        store.put("analyses",analysis)
        for collection,values in (("nodes",nodes),("edges",edges),("files",files)):
            store.put_many(collection,values)
    store.put("users",{"id":"one","login":"one"})
    store.put("users",{"id":"two","login":"two"})
    for id in ("a","b"):
        store.put("user_analyses",{"id":f"one:{id}","userId":"one","analysisId":id,"repositoryId":"repo"})
    store.put("user_analyses",{"id":"two:a","userId":"two","analysisId":"a","repositoryId":"repo"})
    monkeypatch.setattr(auth,"require_identity",lambda *_:store.one("users",{"id": "two" if _[1].headers.get("x-test-user") == "two" else "one"}))
    app = main.create_app(config)
    with TestClient(app) as client:
        yield client,store


def test_exploration_endpoints_enforce_analysis_access(secured):
    client,_ = secured
    assert client.get('/api/v1/analyses/a/impact',params={"node":"fn"}).status_code == 200
    assert client.get('/api/v1/analyses/a/tour').json()["steps"]
    assert {item["id"] for item in client.get('/api/v1/analyses/a/snapshots').json()["snapshots"]} == {"a","b"}
    assert client.get('/api/v1/analyses/a/compare',params={"base":"b"}).status_code == 200
    assert client.get('/api/v1/analyses/a/compare',params={"base":"private"}).status_code == 404
    assert client.get('/api/v1/analyses/private/impact',params={"node":"fn"}).status_code == 404
    assert client.get('/api/v1/analyses/a/impact',params={"node":"foreign"}).status_code == 404
    assert client.get('/api/v1/analyses/a/compare/file',params={"base":"b","path":"src/main.ts"}).status_code == 200
    assert client.get('/api/v1/analyses/a/nodes/fn').json()["name"] == "run"


def test_saved_investigations_are_persistent_owner_scoped_and_validate_ranges(secured):
    client,store = secured
    body = {"title":"Investigate service","notes":"Review callers","view":{"selectedNodeId":"fn","startLine":1,"endLine":2,"layout":"source"},"question":"Why?","answerText":"An answer","citations":[{"fileId":"file:src/main.ts","startLine":1,"endLine":1}]}
    created = client.post('/api/v1/analyses/a/investigations',json=body)
    assert created.status_code == 201
    item = created.json()
    assert "userId" not in item and item["citations"][0]["analysisId"] == "a"
    assert store.one("investigations",{"id":item["id"]})["userId"] == "one"
    assert client.get('/api/v1/analyses/a/investigations').json()["investigations"][0]["view"]["selectedNodeId"] == "fn"
    assert client.get('/api/v1/analyses/a/investigations',headers={"x-test-user":"two"}).json()["investigations"] == []
    assert client.delete(f'/api/v1/analyses/a/investigations/{item["id"]}',headers={"x-test-user":"two"}).status_code == 404
    assert client.post('/api/v1/analyses/a/investigations',json={**body,"citations":[{"fileId":"file:foreign","startLine":1,"endLine":1}]}).status_code == 400
    assert client.post('/api/v1/analyses/a/investigations',json={**body,"view":{"selectedNodeId":"fn","startLine":30}}).status_code == 400
    assert client.delete(f'/api/v1/analyses/a/investigations/{item["id"]}').status_code == 204


@pytest.mark.parametrize('ref,expected', [('feature/branch','commits/feature%2Fbranch'),('PR #12','pulls/12')])
def test_revision_metadata_resolves_branch_and_pull_request(monkeypatch,ref,expected):
    urls=[]
    class Github:
        def __enter__(self): return self
        def __exit__(self,*_): return None
        def get(self,url):
            urls.append(url)
            data = {"id":1,"html_url":"https://github.com/example/repo","default_branch":"main"} if url.endswith('/repo') else {"head":{"sha":"b"*40}} if '/pulls/' in url else {"sha":"b"*40}
            return httpx.Response(200,json=data,request=httpx.Request('GET',url))
    monkeypatch.setattr('navigator.ingestion.httpx.Client',lambda **_:Github())
    assert github_metadata('https://github.com/example/repo',Settings(_env_file=None),ref)["commitSha"] == "b"*40
    assert any(url.endswith(expected) for url in urls)
