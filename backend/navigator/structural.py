"""Bounded, source-only graph adapters for Python and Java snapshots."""

import ast
import hashlib
import re
from pathlib import PurePosixPath


def _edge_id(edge):
    identity = (edge["source"], edge["target"], edge["kind"], edge.get("fileId"),
                edge.get("startLine"), edge.get("specifier"))
    return "edge:" + hashlib.sha256(repr(identity).encode()).hexdigest()[:24]


class Graph:
    def __init__(self, files):
        self.files = {file["path"]: file for file in files}
        self.nodes = {}
        self.edges = {}
        self.regions = []
        self.unresolved_references = []
        self.diagnostics = []
        self.node({"id": "repo", "kind": "repository", "name": "Repository"})

    def node(self, value):
        self.nodes.setdefault(value["id"], value)

    def edge(self, source, target, kind, *, file=None, line=None, specifier=None, resolution="structural"):
        value = {"source": source, "target": target, "kind": kind, "resolution": resolution}
        if file:
            value.update(fileId=f"file:{file}", startLine=line, endLine=line)
        if specifier:
            value["specifier"] = specifier
        value["id"] = _edge_id(value)
        self.edges.setdefault(value["id"], value)

    def module(self, directory):
        if directory in {"", "."}:
            directory = "."
        key = f"module:{directory}"
        if key not in self.nodes:
            parent = "repo" if directory == "." else self.module(str(PurePosixPath(directory).parent))
            self.node({"id": key, "kind": "module", "name": "Root" if directory == "." else PurePosixPath(directory).name,
                       "path": directory, "parentId": parent})
            self.edge(parent, key, "contains")
        return key

    def file(self, file):
        path = file["path"]
        file_id = file["id"]
        parent = self.module(str(PurePosixPath(path).parent))
        self.node({"id": file_id, "kind": "file", "name": PurePosixPath(path).name, "path": path,
                   "fileId": file_id, "parentId": parent, "language": file["language"],
                   "startLine": 1, "endLine": file["lineCount"], "startByte": 0,
                   "endByte": len(file["content"].encode())})
        self.edge(parent, file_id, "contains")

    def symbol(self, file, name, kind, start, end, start_byte, end_byte, *, parent=None, signature=None, tags=None):
        path = file["path"]
        symbol_id = f"symbol:{path}:{start_byte}:{name}"
        self.node({"id": symbol_id, "kind": "symbol", "symbolKind": kind, "name": name, "path": path,
                   "fileId": file["id"], "parentId": parent or file["id"], "language": file["language"],
                   "startLine": start, "endLine": end, "startByte": start_byte, "endByte": end_byte,
                   "signature": (signature or name)[:400], "exported": not name.startswith("_"), "tags": tags or []})
        self.edge(parent or file["id"], symbol_id, "contains")
        self.regions.append({"fileId": file["id"], "symbolId": symbol_id, "kind": kind,
                             "startLine": start, "endLine": end, "startByte": start_byte,
                             "endByte": end_byte, "signature": (signature or name)[:400]})
        return symbol_id

    def dependency(self, source, target, file, line, specifier):
        if target:
            self.edge(source, f"file:{target}", "imports", file=file, line=line,
                      specifier=specifier, resolution="resolved")
        else:
            root = specifier.lstrip(".").split(".")[0]
            if not root:
                return
            external = f"external:{root}"
            self.node({"id": external, "kind": "external", "name": root})
            self.edge(source, external, "imports", file=file, line=line, specifier=specifier)

    def result(self):
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values()),
                "regions": self.regions, "unresolved_references": self.unresolved_references,
                "diagnostics": self.diagnostics}


def _python_target(path, module, level, available):
    parts = list(PurePosixPath(path).parts[:-1])
    if level:
        parts = parts[:max(0, len(parts) - level + 1)]
    else:
        parts = []
    parts += [part for part in module.split(".") if part]
    stem = "/".join(parts)
    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
        if candidate in available:
            return candidate
    # src-layout packages are commonly imported without their source prefix.
    for candidate in (f"src/{stem}.py", f"src/{stem}/__init__.py"):
        if candidate in available:
            return candidate
    return None


def _python_routes(node, receivers, api_view_names):
    def route_tag(receiver, route):
        combined = f"/{receivers[receiver].strip('/')}/{route.lstrip('/')}".replace("//", "/")
        return f"route:{combined.rstrip('/') or '/'}"

    tags = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        attr = decorator.func
        if isinstance(attr, ast.Name) and attr.id in api_view_names:
            methods = decorator.args[0] if decorator.args else None
            method = methods.elts[0].value if isinstance(methods, (ast.List, ast.Tuple)) and methods.elts and isinstance(methods.elts[0], ast.Constant) else "ROUTE"
            tags += ["api-handler", f"http:{str(method).upper()}"]
            continue
        if not isinstance(attr, ast.Attribute):
            continue
        if not isinstance(attr.value, ast.Name) or attr.value.id not in receivers:
            continue
        method = attr.attr.lower()
        if method in {"get", "post", "put", "patch", "delete", "head", "options", "api_view"}:
            route = decorator.args[0].value if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str) else None
            tags += ["api-handler", f"http:{method.upper()}"]
            if route is not None:
                tags.append(route_tag(attr.value.id, route))
        elif method == "route":
            route = decorator.args[0].value if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str) else None
            tags += ["api-handler", "http:ROUTE"]
            if route is not None:
                tags.append(route_tag(attr.value.id, route))
    return list(dict.fromkeys(tags))


def analyze_python(files):
    graph = Graph(files)
    available = graph.files
    for file in files:
        graph.file(file)
        path, source = file["path"], file["content"]
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as error:
            graph.diagnostics.append({"severity": "warning", "path": path, "line": error.lineno or 1,
                                      "message": f"Python syntax could not be parsed: {path}:{error.lineno or 1}"})
            continue
        lines = source.splitlines(keepends=True)
        byte_starts = [0]
        for line in lines:
            byte_starts.append(byte_starts[-1] + len(line.encode()))
        names = {}
        declarations = []
        django_url_helpers = set()
        route_receivers = {}
        api_view_names = set()
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == "rest_framework.decorators":
                api_view_names.update(alias.asname or alias.name for alias in statement.names if alias.name == "api_view")
            value = statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target] if isinstance(statement, ast.AnnAssign) else []
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id not in {"FastAPI", "APIRouter", "Flask", "Blueprint"}:
                continue
            prefix = next((keyword.value.value for keyword in value.keywords
                           if keyword.arg in {"prefix", "url_prefix"} and isinstance(keyword.value, ast.Constant)
                           and isinstance(keyword.value.value, str)), "")
            route_receivers.update({target.id: prefix for target in targets if isinstance(target, ast.Name)})

        def visit(body, parent=None):
            for node in body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                end = node.end_lineno or node.lineno
                start_byte = byte_starts[start - 1]
                end_byte = byte_starts[end]
                kind = "class" if isinstance(node, ast.ClassDef) else "method" if parent else "function"
                signature = lines[node.lineno - 1].strip()
                symbol = graph.symbol(file, node.name, kind, start, end, start_byte, end_byte,
                                      parent=parent, signature=signature, tags=_python_routes(node, route_receivers, api_view_names))
                if not parent:
                    names[node.name] = symbol
                declarations.append((node, symbol))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, symbol)
        visit(tree.body)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    graph.dependency(file["id"], _python_target(path, alias.name, 0, available), path, node.lineno, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "django.urls":
                    django_url_helpers.update(alias.asname or alias.name for alias in node.names if alias.name in {"path", "re_path"})
                target = _python_target(path, module, node.level, available) if module else None
                if not target:
                    for alias in node.names:
                        target = _python_target(path, f"{module}.{alias.name}".strip("."), node.level, available)
                        if target:
                            break
                if target:
                    graph.dependency(file["id"], target, path, node.lineno, "." * node.level + module)
                elif node.level:
                    graph.unresolved_references.append({"fileId": file["id"], "startLine": node.lineno,
                                                         "endLine": node.lineno, "specifier": "." * node.level + module,
                                                         "reason": "Relative Python import has no source target in the snapshot."})
                else:
                    graph.dependency(file["id"], None, path, node.lineno, module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
                owners = [(declaration.end_lineno - declaration.lineno, symbol)
                          for declaration, symbol in declarations
                          if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and declaration.lineno <= node.lineno <= declaration.end_lineno]
                source_id = min(owners)[1] if owners else file["id"]
                if source_id != names[node.func.id]:
                    graph.edge(source_id, names[node.func.id], "calls", file=path, line=node.lineno, resolution="resolved")
        if django_url_helpers:
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in django_url_helpers and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    line = node.lineno
                    start_byte = byte_starts[line - 1] + node.col_offset
                    end_byte = byte_starts[node.end_lineno - 1] + node.end_col_offset
                    graph.symbol(file, f"route@{line}", "route", line, node.end_lineno,
                                 start_byte, end_byte, signature=lines[line - 1].strip(),
                                 tags=["api-handler", "http:ROUTE", f"route:{node.args[0].value}"])
    return graph.result()


_JAVA_IMPORT = re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;")
_JAVA_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
_JAVA_TYPE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)\b")
_JAVA_SIGNATURE = re.compile(r"([A-Za-z_$][\w$]*)\s*\([^(){};]{0,400}\)\s*(?:throws\s+[\w.$,\s]{1,160})?$")
_JAVA_ROUTE = re.compile(r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\s*(?:\(\s*(?:value\s*=\s*|path\s*=\s*)?[\"']([^\"']*)[\"'])?")


def _mask_java(source):
    pattern = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|\"\"\"[\s\S]*?\"\"\"|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
    return pattern.sub(lambda m: "".join("\n" if char == "\n" else " " for char in m.group()), source)


def _brace_pairs(masked):
    stack, ends = [], {}
    for index, char in enumerate(masked):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            ends[stack.pop()] = index + 1
    return ends


def _java_annotations(source, masked, start, end):
    return [match for match in _JAVA_ROUTE.finditer(source, start, end) if masked[match.start()] == "@"]


def analyze_java(files):
    graph = Graph(files)
    packages = {}
    for file in files:
        match = _JAVA_PACKAGE.search(_mask_java(file["content"]))
        if match:
            packages[f"{match.group(1)}.{PurePosixPath(file['path']).stem}"] = file["path"]
    for file in files:
        graph.file(file)
        path, source = file["path"], file["content"]
        masked = _mask_java(source)
        brace_ends = _brace_pairs(masked)
        for match in _JAVA_IMPORT.finditer(masked):
            specifier = match.group(1)
            target = packages.get(specifier)
            if target is None:
                target = next((p for qualified, p in packages.items() if specifier.startswith(qualified + ".")), None)
            if target is None and specifier.endswith(".*"):
                target = next((p for qualified, p in packages.items() if qualified.startswith(specifier[:-1])), None)
            graph.dependency(file["id"], target, path, source.count("\n", 0, match.start()) + 1, specifier)
        classes = {}
        for match in _JAVA_TYPE.finditer(masked):
            opening = masked.find("{", match.end(), match.end() + 400)
            if opening < 0 or ";" in masked[match.end():opening]:
                continue
            end = brace_ends.get(opening, len(masked))
            start_line, end_line = source.count("\n", 0, match.start()) + 1, source.count("\n", 0, max(0, end - 1)) + 1
            start_byte, end_byte = len(source[:match.start()].encode()), len(source[:end].encode())
            previous = max(source.rfind(";", 0, match.start()), source.rfind("}", 0, match.start()), match.start() - 400)
            class_prefix = source[previous + 1:match.start()]
            stereotypes = [name for name in ("RestController", "Controller", "Service", "Repository", "Component", "Configuration")
                           if any(masked[previous + 1 + item.start()] == "@" for item in re.finditer(rf"@{name}\b", class_prefix))]
            symbol = graph.symbol(file, match.group(2), match.group(1), start_line, end_line,
                                  start_byte, end_byte, signature=source[match.start():opening].strip(),
                                  tags=[f"spring:{name.lower()}" for name in stereotypes])
            class_annotations = _java_annotations(source, masked, previous + 1, match.start())
            base_route = class_annotations[-1].group(2) if class_annotations and class_annotations[-1].group(1) == "RequestMapping" else None
            classes[opening] = (opening, end, symbol, base_route)
        stack = []
        for opening, char in enumerate(masked):
            if char == "{":
                owner = classes.get(stack[-1]) if stack else None
                if owner and opening not in classes:
                    previous = max(masked.rfind(";", owner[0] + 1, opening),
                                   masked.rfind("}", owner[0] + 1, opening), owner[0])
                    segment = masked[previous + 1:opening]
                    signature = _JAVA_SIGNATURE.search(segment.strip()) if len(segment) <= 800 else None
                    if signature and signature.group(1) not in {"if", "for", "while", "switch", "catch", "synchronized"}:
                        start = previous + 1 + len(segment) - len(segment.lstrip())
                        end = brace_ends.get(opening, len(masked))
                        annotations = _java_annotations(source, masked, start, opening)
                        tags = []
                        if annotations:
                            last = annotations[-1]
                            method = last.group(1).removesuffix("Mapping").upper() or "ROUTE"
                            tags = ["api-handler", f"http:{method}"]
                            if last.group(2) is not None:
                                route = last.group(2)
                                if owner[3]:
                                    route = f"/{owner[3].strip('/')}/{route.lstrip('/')}".rstrip("/") or "/"
                                tags.append(f"route:{route}")
                        graph.symbol(file, signature.group(1), "method", source.count("\n", 0, start) + 1,
                                     source.count("\n", 0, max(0, end - 1)) + 1, len(source[:start].encode()),
                                     len(source[:end].encode()), parent=owner[2],
                                     signature=source[start:opening].strip(), tags=tags)
                stack.append(opening)
            elif char == "}" and stack:
                stack.pop()
    return graph.result()
