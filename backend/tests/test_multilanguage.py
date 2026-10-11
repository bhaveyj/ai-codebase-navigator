import pytest

from navigator.analysis import run_analyzer, overview
from navigator.config import Settings
from navigator.ingestion import eligible, make_file
from navigator.jobs import create_job, run_job
from navigator.storage import LocalStore


def source(path, content):
    return make_file(path, content.encode(), "analysis")


def test_python_fastapi_imports_routes_and_chunks_without_node():
    files = [
        source("app/main.py", "from fastapi import FastAPI\nfrom .service import answer\napp = FastAPI()\n@app.get('/hello')\ndef hello():\n    return answer()\n"),
        source("app/service.py", "def answer():\n    return {'hello': True}\n"),
        source("pyproject.toml", '[project]\ndependencies = ["fastapi>=0.115", "sqlalchemy>=2"]\n'),
    ]
    records = run_analyzer(files, "analysis", Settings(_env_file=None, NODE_BINARY="missing-node"))
    nodes = {node["id"]: node for node in records["nodes"]}
    assert nodes["file:app/main.py"]["language"] == "python"
    assert any(node.get("name") == "hello" and "route:/hello" in node.get("tags", []) for node in nodes.values())
    assert any(edge["source"] == "file:app/main.py" and edge["target"] == "file:app/service.py" for edge in records["edges"])
    assert any(region["kind"] == "function" and region["startLine"] == 4 for region in records["regions"])
    assert "fastapi" in overview(files, records["nodes"])["frameworks"]
    assert "sqlalchemy" in overview(files, records["nodes"])["integrations"]


def test_java_spring_imports_controller_and_routes_without_node():
    files = [
        source("src/main/java/com/acme/Application.java", "package com.acme;\nimport com.acme.Service;\nimport org.springframework.boot.autoconfigure.SpringBootApplication;\n@SpringBootApplication\n@RequestMapping(\"/api\")\npublic class Application {\n @GetMapping(\"/hello\")\n public String hello() { return \"hello\"; }\n}\n"),
        source("src/main/java/com/acme/Service.java", "package com.acme;\npublic class Service {}\n"),
        source("pom.xml", "<project><dependency><artifactId>spring-boot-starter-web</artifactId></dependency></project>"),
    ]
    records = run_analyzer(files, "analysis", Settings(_env_file=None, NODE_BINARY="missing-node"))
    assert any(node.get("name") == "Application" and node.get("symbolKind") == "class" for node in records["nodes"])
    assert any(node.get("name") == "hello" and "route:/api/hello" in node.get("tags", []) for node in records["nodes"])
    assert next(node for node in records["nodes"] if node.get("name") == "hello")["startLine"] == 7
    assert any(edge["source"] == "file:src/main/java/com/acme/Application.java" and edge["target"] == "file:src/main/java/com/acme/Service.java" for edge in records["edges"])
    assert "Spring Boot" in overview(files, records["nodes"])["frameworks"]


def test_ingestion_includes_java_python_and_manifests():
    for path in ("src/main.py", "src/App.java", "pyproject.toml", "requirements-dev.txt", "pom.xml", "build.gradle.kts"):
        assert eligible(path)
    assert make_file("src/main.py", b"print('ok')", "analysis")["language"] == "python"
    assert make_file("src/App.java", b"class App {}", "analysis")["language"] == "java"


def test_django_urlpattern_is_exposed_as_route():
    files = [source("site/urls.py", "from django.urls import path\nfrom .views import home\nurlpatterns = [path('home/', home)]\n"),
             source("site/views.py", "def home(request):\n    return None\n")]
    records = run_analyzer(files, "analysis", Settings(_env_file=None, NODE_BINARY="missing-node"))
    assert any("route:home/" in node.get("tags", []) for node in records["nodes"])


def test_python_routes_require_a_framework_receiver():
    files = [source("api.py", "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api')\n@router.get('/items')\ndef items():\n    return []\n@cache.get('/fake')\ndef unrelated():\n    return []\n")]
    records = run_analyzer(files, "analysis", Settings(_env_file=None, NODE_BINARY="missing-node"))
    symbols = {node["name"]: node for node in records["nodes"] if node["kind"] == "symbol"}
    assert "route:/api/items" in symbols["items"]["tags"]
    assert not symbols["unrelated"]["tags"]


@pytest.mark.parametrize("filename,content,language", [
    ("main.py", "def main():\n    return 1\n", "python"),
    ("Main.java", "public class Main { public static void main(String[] args) {} }", "java"),
])
def test_language_only_repository_job_reaches_ready(tmp_path, filename, content, language):
    fixture = tmp_path / "repository"
    fixture.mkdir()
    (fixture / filename).write_text(content, encoding="utf-8")
    settings = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_DATA_DIR=tmp_path / "data",
                        NAVIGATOR_DEMO_DIR=fixture, NODE_BINARY="missing-node", GEMINI_API_KEY="")
    store = LocalStore(settings.data_dir)
    metadata = {"owner": "example", "name": "source", "url": "https://github.com/example/source",
                "defaultBranch": "main", "commitSha": "a" * 40}
    payload, created = create_job(store, metadata, settings, demo=True)
    assert created
    run_job(payload["job"]["id"], settings)
    analysis = store.one("analyses", {"id": payload["analysis"]["id"]})
    assert analysis["status"] == "ready" and analysis["graphReady"]
    assert analysis["languages"] == [language]
    assert store.find("chunks", {"analysisId": analysis["id"]})
