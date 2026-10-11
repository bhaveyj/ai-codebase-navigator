import hashlib
import json
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import PurePosixPath

from pydantic import ValidationError

from .config import Settings
from .contracts import DomainError, Edge, Node, Region
from .structural import analyze_java, analyze_python


def _run_typescript_analyzer(files, analysis_id: str, settings: Settings, check_cancel=lambda: None):
    if not settings.analyzer.is_file():
        raise DomainError("ANALYZER_UNAVAILABLE", "TypeScript analyzer is missing. Build/install the analyzer before starting a job.", 503)
    request = {"protocolVersion": 1, "analysisId": analysis_id, "rootPath": "/snapshot", "files": [{"path": file["path"], "content": file["content"], "language": file["language"]} for file in files]}
    # Only runtime environment reaches the trusted analyzer. No service credentials.
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        stdin.write(json.dumps(request).encode())
        stdin.seek(0)
        process = subprocess.Popen([settings.node_binary, f"--max-old-space-size={settings.analyzer_memory_mb}", str(settings.analyzer)], stdin=stdin, stdout=stdout, stderr=stderr, env=environment)
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancel()
                if time.monotonic() - started > settings.analyzer_timeout:
                    raise DomainError("ANALYSIS_TIMEOUT", "Static analysis exceeded its time limit. Try a smaller repository.")
                if stdout.tell() > 64 * 1024 * 1024 or stderr.tell() > 2 * 1024 * 1024:
                    raise DomainError("ANALYZER_OUTPUT_LIMIT", "Analyzer output exceeded its resource limit.")
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode:
            raise DomainError("ANALYZER_FAILED", "Static analysis failed. Check that analyzer dependencies are installed and the Node runtime is available.", 500)
        stdout.seek(0)
        records = {"nodes": [], "edges": [], "regions": [], "unresolved_references": [], "diagnostics": []}
        complete = None
        valid_files = {file["id"]: file for file in files}
        mapping = {"node": "nodes", "edge": "edges", "region": "regions", "unresolved": "unresolved_references", "diagnostic": "diagnostics"}
        try:
            for line in stdout:
                if complete is not None:
                    raise ValueError("Data after completion")
                record = json.loads(line)
                record_type = record.pop("type")
                if record_type == "complete":
                    if record.get("protocolVersion") != 1:
                        raise ValueError("Unsupported analyzer protocol")
                    complete = record
                    continue
                if record_type not in mapping:
                    raise ValueError("Unknown record type")
                if record_type == "node":
                    record = Node.model_validate(record).model_dump(exclude_none=True)
                elif record_type == "edge":
                    record = Edge.model_validate(record).model_dump(exclude_none=True)
                elif record_type == "region":
                    record = Region.model_validate(record).model_dump(exclude_none=True)
                if record.get("fileId") and record["fileId"] not in valid_files:
                    raise ValueError("Source file outside approved manifest")
                if "startLine" in record and "fileId" in record:
                    file = valid_files[record["fileId"]]
                    if not 1 <= record["startLine"] <= record["endLine"] <= file["lineCount"]:
                        raise ValueError("Invalid source range")
                    if "startByte" in record and not 0 <= record["startByte"] <= record["endByte"] <= len(file["content"].encode()):
                        raise ValueError("Invalid byte range")
                record["analysisId"] = analysis_id
                record.setdefault("id", hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24])
                records[mapping[record_type]].append(record)
            if complete is None:
                raise ValueError("Incomplete analyzer output")
            expected = {key: len(records[key]) for key in ("nodes", "edges", "regions")}
            if complete.get("counts") != expected:
                raise ValueError("Analyzer completion counts do not match records")
            ids = {node["id"] for node in records["nodes"]}
            if len(ids) != len(records["nodes"]):
                raise ValueError("Duplicate node ID")
            for edge in records["edges"]:
                if edge["source"] not in ids or edge["target"] not in ids:
                    raise ValueError("Dangling edge")
        except (ValueError, KeyError, ValidationError) as error:
            raise DomainError("INVALID_ANALYZER_OUTPUT", "Analyzer output failed schema or source-reference validation.", 500) from error
        return records


def run_analyzer(files, analysis_id: str, settings: Settings, check_cancel=lambda: None):
    """Combine independent language adapters in the shared graph contract."""
    records = {"nodes": [], "edges": [], "regions": [], "unresolved_references": [], "diagnostics": []}
    for language in ("javascript", "python", "java"):
        check_cancel()
        selected = ([file for file in files if file["language"] in {"javascript", "typescript"}]
                    if language == "javascript" else [file for file in files if file["language"] == language])
        if not selected:
            continue
        if language == "javascript":
            result = _run_typescript_analyzer(files, analysis_id, settings, check_cancel)
        else:
            result = analyze_python(selected) if language == "python" else analyze_java(selected)
        for key, values in result.items():
            for record in values:
                if language != "javascript":
                    if key == "nodes":
                        record = Node.model_validate(record).model_dump(exclude_none=True)
                    elif key == "edges":
                        record = Edge.model_validate(record).model_dump(exclude_none=True)
                    elif key == "regions":
                        record = Region.model_validate(record).model_dump(exclude_none=True)
                    record["analysisId"] = analysis_id
                    record.setdefault("id", hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24])
                records[key].append(record)
    # Shared repository and directory nodes may be emitted by multiple adapters.
    node_ids = set()
    records["nodes"] = [node for node in records["nodes"] if not (node["id"] in node_ids or node_ids.add(node["id"]))]
    edge_keys = set()
    records["edges"] = [edge for edge in records["edges"] if not (
        (key := (edge["source"], edge["target"], edge["kind"], edge.get("fileId"), edge.get("startLine"), edge.get("specifier"))) in edge_keys
        or edge_keys.add(key))]
    if any(edge["source"] not in node_ids or edge["target"] not in node_ids for edge in records["edges"]):
        raise DomainError("INVALID_ANALYZER_OUTPUT", "Analyzer returned a relationship outside the source graph.", 500)
    return records


def build_chunks(files, regions, nodes, settings: Settings):
    by_file = {}
    for region in regions:
        by_file.setdefault(region["fileId"], []).append(region)
    symbols = {node["id"]: node for node in nodes}
    chunks = []
    for file in files:
        lines = file["content"].splitlines(keepends=True)
        if not lines:
            continue
        # Prefer the smallest declaration regions, avoiding parent/child duplication.
        selected, covered = [], set()
        candidates = sorted(by_file.get(file["id"], []), key=lambda r: (r["endLine"] - r["startLine"], r["startLine"]))
        for region in candidates:
            occupied = set(range(region["startLine"], region["endLine"] + 1))
            if not occupied.intersection(covered):
                selected.append(region)
                covered.update(occupied)
        # Index imports and remaining top-level statements as coherent uncovered runs.
        run_start = None
        for line_no in range(1, len(lines) + 2):
            uncovered = line_no <= len(lines) and line_no not in covered
            if uncovered and run_start is None:
                run_start = line_no
            if not uncovered and run_start is not None:
                selected.append({"startLine": run_start, "endLine": line_no - 1, "kind": "documentation" if file["language"] == "markdown" else "top-level"})
                run_start = None
        for region in sorted(selected, key=lambda r: r["startLine"]):
            start = region["startLine"]
            text_parts, char_count = [], 0
            for line_no in range(start, min(region["endLine"], len(lines)) + 1):
                line = lines[line_no - 1]
                if text_parts and char_count + len(line) > 4800:
                    _append_chunk(chunks, file, region, symbols, start, line_no - 1, "".join(text_parts), settings)
                    text_parts, char_count, start = [], 0, line_no
                # A single generated-style long line gets bounded pieces, all cited to that line.
                if len(line) > 8000:
                    if text_parts:
                        _append_chunk(chunks, file, region, symbols, start, line_no - 1, "".join(text_parts), settings)
                        text_parts, char_count = [], 0
                    for offset in range(0, len(line), 4800):
                        _append_chunk(chunks, file, region, symbols, line_no, line_no, line[offset:offset + 4800], settings, offset)
                    start = line_no + 1
                    continue
                text_parts.append(line)
                char_count += len(line)
            if text_parts:
                _append_chunk(chunks, file, region, symbols, start, min(region["endLine"], len(lines)), "".join(text_parts), settings)
    if settings.chunk_profile_version >= 2:
        chunks = _group_small_chunks(chunks, files, settings)
    if len(chunks) > settings.max_chunks:
        raise DomainError("CHUNK_LIMIT", "Repository exceeds the 15,000-chunk limit. Try a smaller repository.")
    return chunks


def _group_type(chunk):
    if len(chunk["text"]) > 700:
        return None
    source = chunk["text"].lstrip()
    if source.startswith("import ") or source.startswith("export * from ") or (source.startswith("export { ") and " from " in source):
        return "imports"
    if chunk.get("symbolId"):
        return "declarations"
    if chunk["kind"] in {"statement", "top-level"}:
        return "statements"
    return None


def _group_small_chunks(chunks, files, settings):
    """Pack nearby small regions while retaining an exact citable source span."""
    by_file = {file["id"]: file for file in files}
    lines_by_file = {}
    offsets_by_file = {}

    def source_lines(file_id):
        if file_id not in lines_by_file:
            lines = by_file[file_id]["content"].splitlines(keepends=True)
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line))
            lines_by_file[file_id] = lines
            offsets_by_file[file_id] = offsets
        return lines_by_file[file_id], offsets_by_file[file_id]

    result = []
    group = []

    def flush():
        if not group:
            return
        if len(group) == 1:
            result.append(group[0])
            group.clear()
            return
        file = by_file[group[0]["fileId"]]
        start, end = group[0]["startLine"], group[-1]["endLine"]
        lines, _ = source_lines(file["id"])
        source = "".join(lines[start - 1:end])
        names = list(dict.fromkeys(chunk["symbol"] for chunk in group if chunk.get("symbol")))
        region = {"kind": _group_type(group[0]), "signature": ", ".join(names[:8])}
        _append_chunk(result, file, region, {}, start, end, source, settings)
        group.clear()

    for chunk in chunks:
        kind = _group_type(chunk)
        if group:
            last = group[-1]
            same_file = chunk["fileId"] == last["fileId"]
            proposed_chars = 1801
            if same_file:
                _, offsets = source_lines(chunk["fileId"])
                proposed_chars = offsets[chunk["endLine"]] - offsets[group[0]["startLine"] - 1]
            if (not kind or kind != _group_type(group[0]) or not same_file
                    or chunk["startLine"] > last["endLine"] + 3 or proposed_chars > 1800):
                flush()
        if kind:
            group.append(chunk)
        else:
            result.append(chunk)
    flush()
    return result


def _append_chunk(chunks, file, region, symbols, start, end, text, settings, offset=0):
    if not text.strip():
        return
    symbol = symbols.get(region.get("symbolId"), {})
    header = f"File: {file['path']}\nLanguage: {file['language']}\nSymbol: {symbol.get('name', '')}\nSignature: {region.get('signature', '')}\nLines: {start}-{end}\n"
    embedding_text = header + text
    digest = hashlib.sha256((settings.embedding_config + embedding_text).encode()).hexdigest()
    chunks.append({"id": f"chunk:{file['path']}:{start}:{end}:{offset}:{digest[:8]}", "analysisId": file["analysisId"], "fileId": file["id"], "path": file["path"], "language": file["language"], "symbolId": region.get("symbolId"), "symbol": symbol.get("name", ""), "kind": region["kind"], "startLine": start, "endLine": end, "text": text, "embeddingText": embedding_text, "inputHash": digest, "embeddingConfig": settings.embedding_config})


def overview(files, nodes):
    frameworks, integrations, entries, routes = set(), set(), [], []
    dependencies = set()
    for file in files:
        if PurePosixPath(file["path"]).name == "package.json":
            try:
                package = json.loads(file["content"])
                dependencies.update(package.get("dependencies", {}))
                dependencies.update(package.get("devDependencies", {}))
                for key in ("main", "module", "browser"):
                    if isinstance(package.get(key), str):
                        entries.append(str(PurePosixPath(file["path"]).parent / package[key]))
            except (ValueError, TypeError):
                pass
        if PurePosixPath(file["path"]).stem in {"main", "index", "server", "app"} and file["language"] in {"javascript", "typescript"}:
            entries.append(file["path"])
        name = PurePosixPath(file["path"]).name.lower()
        if file["language"] == "python":
            if name in {"main.py", "app.py", "manage.py", "asgi.py", "wsgi.py"}:
                entries.append(file["path"])
            for framework in ("django", "fastapi", "flask", "starlette"):
                if f"import {framework}" in file["content"] or f"from {framework}" in file["content"]:
                    frameworks.add(framework)
        if file["language"] == "java":
            if "@SpringBootApplication" in file["content"] or "static void main(" in file["content"]:
                entries.append(file["path"])
            if "org.springframework" in file["content"]:
                frameworks.add("Spring Boot" if "@SpringBootApplication" in file["content"] else "Spring")
        if name in {"pyproject.toml", "pipfile"}:
            try:
                project = tomllib.loads(file["content"])
                dependencies.update(str(item).split("[")[0].split("=")[0].split(">")[0].split("<")[0].lower()
                                    for item in project.get("project", {}).get("dependencies", []))
                dependencies.update(project.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys())
                dependencies.update(key.lower() for key in project.get("packages", {}))
                dependencies.update(key.lower() for key in project.get("dev-packages", {}))
            except (ValueError, TypeError, AttributeError):
                pass
        if name.startswith("requirements") and name.endswith(".txt"):
            dependencies.update(line.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip().lower()
                                for line in file["content"].splitlines() if line.strip() and not line.lstrip().startswith(("#", "-")))
        if name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
            for marker, framework in (("spring-boot", "Spring Boot"), ("quarkus", "Quarkus"), ("micronaut", "Micronaut")):
                if marker in file["content"].lower():
                    frameworks.add(framework)
            for marker, integration in (("spring-boot-starter-data-jpa", "Spring Data JPA"),
                                        ("spring-boot-starter-security", "Spring Security"),
                                        ("hibernate", "Hibernate"), ("postgresql", "PostgreSQL"),
                                        ("mysql", "MySQL"), ("kafka", "Kafka"), ("redis", "Redis")):
                if marker in file["content"].lower():
                    integrations.add(integration)
    frameworks.update(name for name in ("react", "next", "express", "fastify", "vue", "svelte", "@nestjs/core") if name in dependencies)
    frameworks.update(name for name in ("django", "fastapi", "flask", "starlette") if name in dependencies)
    integrations.update(name for name in ("mongodb", "mongoose", "prisma", "@prisma/client", "pg", "redis", "ioredis", "passport", "next-auth", "@auth/core", "stripe", "firebase", "@supabase/supabase-js") if name in dependencies)
    integrations.update(name for name in ("sqlalchemy", "psycopg", "psycopg2", "pymongo", "celery", "redis", "boto3", "requests", "httpx") if name in dependencies)
    for node in nodes:
        if "api-handler" in node.get("tags", []) or node.get("symbolKind") in {"api-handler", "route"}:
            tags = node.get("tags", [])
            method = next((tag[5:] for tag in tags if tag.startswith("http:")), "ROUTE")
            route = next((tag[6:] for tag in tags if tag.startswith("route:")), None)
            routes.append({"nodeId": node["id"], "path": node.get("path"), "filePath": node.get("path"), "name": node["name"], "method": method, "route": route, "startLine": node.get("startLine")})
    return {"frameworks": sorted(frameworks), "entryPoints": list(dict.fromkeys(entries))[:30], "routes": routes[:100], "integrations": sorted(integrations)}
