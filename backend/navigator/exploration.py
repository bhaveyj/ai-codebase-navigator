"""Source-backed exploration over immutable analysis snapshots; no model calls."""
import difflib
import hashlib
import re
from collections import defaultdict, deque

from .graph import scoped_node


def file_id(node):
    return node["id"] if node["kind"] == "file" else node.get("fileId")


def file_relationships(nodes, edges):
    by_id = {node["id"]: node for node in nodes}
    relationships = {}
    for edge in edges:
        if edge["kind"] == "contains":
            continue
        source = file_id(by_id.get(edge["source"], {"kind": "external"}))
        target = file_id(by_id.get(edge["target"], {"kind": "external"}))
        if source and target and source != target:
            relationships.setdefault((source, target), edge)
    return by_id, relationships


def citation(analysis, files, edge):
    file = files.get(edge.get("fileId"))
    start = edge.get("startLine")
    end = edge.get("endLine", start)
    if not file or not start or not end or not 1 <= start <= end <= file["lineCount"]:
        return None
    return {"id": "E" + hashlib.sha256(edge["id"].encode()).hexdigest()[:12],
            "analysisId": analysis["id"], "commitSha": analysis["commitSha"],
            "fileId": file["id"], "path": file["path"], "startLine": start, "endLine": end}


def impact_analysis(analysis, nodes, edges, files, node_id, depth=3):
    selected = scoped_node(nodes, node_id)
    root = file_id(selected)
    by_id, relationships = file_relationships(nodes, edges)
    if not root:
        from .contracts import DomainError
        raise DomainError("FILE_REQUIRED", "Select a file or symbol to inspect its change impact.")
    incoming = defaultdict(list)
    for (source, target), edge in relationships.items():
        incoming[target].append((source, edge))
    seen, queue, found = {root}, deque([(root, 0)]), []
    file_map = {file["id"]: file for file in files}
    omitted = set()
    while queue:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        for source, edge in sorted(incoming[current], key=lambda item: item[0]):
            if source in seen:
                continue
            if len(seen) >= 501:
                omitted.add(source)
                continue
            seen.add(source)
            queue.append((source, distance + 1))
            found.append({"node": by_id[source], "distance": distance + 1,
                          "via": by_id[current].get("path", by_id[current]["name"]),
                          "relationship": edge["kind"], "citation": citation(analysis, file_map, edge)})
    paths = {by_id[id].get("path") for id in seen if id in by_id}
    routes = [route for route in analysis.get("overview", {}).get("routes", [])
              if (route.get("filePath") or route.get("file") or route.get("path") or str(route.get("fileId", "")).removeprefix("file:")) in paths]
    tests = [item for item in found if re.search(r"(^|/)(tests?|__tests__)/|(^|/)(test_[^/]+|[^/]+[._](test|spec))\.", item["node"].get("path", ""), re.I)]
    return {"selected": selected, "direct": [item for item in found if item["distance"] == 1],
            "transitive": [item for item in found if item["distance"] > 1], "tests": tests,
            "routes": routes, "omitted": len(omitted), "depth": depth,
            "warnings": ["Potential impact follows static file dependencies, including imports and resolved symbol relationships. Runtime behavior and tests without a recorded dependency may be missing."]
            + (["This symbol is evaluated at file scope so callers through its containing file are included."] if selected["kind"] == "symbol" else [])}


def node_identity(node):
    # Lines and analyzer IDs can move between commits without changing a symbol.
    return (node["kind"], node.get("path", ""), node["name"], node.get("symbolKind", ""))


def compare_snapshots(base, current, base_files, current_files, base_nodes, current_nodes, base_edges, current_edges):
    before = {file["path"]: file for file in base_files}
    after = {file["path"]: file for file in current_files}
    changes = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old and new and old["contentHash"] == new["contentHash"]:
            continue
        changes.append({"path": path, "status": "added" if not old else "removed" if not new else "modified",
                        "beforeLines": old["lineCount"] if old else 0, "afterLines": new["lineCount"] if new else 0})
    old_nodes = {node_identity(node): node for node in base_nodes if node["kind"] == "symbol"}
    new_nodes = {node_identity(node): node for node in current_nodes if node["kind"] == "symbol"}
    def normalized_edges(nodes, edges):
        by_id = {node["id"]: node for node in nodes}
        result = {}
        for edge in edges:
            if edge["kind"] == "contains" or edge["source"] not in by_id or edge["target"] not in by_id:
                continue
            source, target = by_id[edge["source"]], by_id[edge["target"]]
            key = (node_identity(source), node_identity(target), edge["kind"])
            result[key] = {"source": source.get("path") or source["name"], "sourceName": source["name"],
                           "target": target.get("path") or target["name"], "targetName": target["name"], "kind": edge["kind"]}
        return result
    old_edges, new_edges = normalized_edges(base_nodes, base_edges), normalized_edges(current_nodes, current_edges)
    added_edges = [new_edges[key] for key in sorted(new_edges.keys() - old_edges.keys())]
    removed_edges = [old_edges[key] for key in sorted(old_edges.keys() - new_edges.keys())]
    return {"base": base, "current": current, "files": changes,
            "symbolsAdded": len(new_nodes.keys() - old_nodes.keys()), "symbolsRemoved": len(old_nodes.keys() - new_nodes.keys()),
            "relationshipsAdded": added_edges[:250], "relationshipsRemoved": removed_edges[:250],
            "relationshipsAddedCount": len(added_edges), "relationshipsRemovedCount": len(removed_edges),
            "omittedRelationships": max(0, len(added_edges) - 250) + max(0, len(removed_edges) - 250),
            "warnings": ["Comparison covers analyzed source only. Renames appear as a removal and an addition. Relationships are matched by symbol name, kind, and path; overloaded symbols with the same name are grouped."]}


def source_diff(before, after, path, limit=1200):
    lines = list(difflib.unified_diff((before or {}).get("content", "").splitlines(),
                                    (after or {}).get("content", "").splitlines(),
                                    fromfile=f"before/{path}", tofile=f"after/{path}", lineterm="", n=4))
    return {"path": path, "lines": lines[:limit], "omittedLines": max(0, len(lines) - limit),
            "redacted": bool((before or {}).get("redacted") or (after or {}).get("redacted"))}


def architecture_tour(analysis, nodes, edges, files, start=None):
    by_id, relationships = file_relationships(nodes, edges)
    starts = []
    for path in analysis.get("overview", {}).get("entryPoints", []):
        if f"file:{path}" in by_id:
            starts.append(by_id[f"file:{path}"])
    for route in analysis.get("overview", {}).get("routes", []):
        path = route.get("filePath") or route.get("file") or str(route.get("fileId", "")).removeprefix("file:")
        node = by_id.get(f"file:{path}")
        if node and node not in starts:
            starts.append(node)
    if not starts:
        starts = sorted([node for node in nodes if node["kind"] == "file"], key=lambda node: node.get("path", ""))[:8]
    root = scoped_node(nodes, start) if start else (starts[0] if starts else None)
    root = by_id.get(file_id(root)) if root else None
    outgoing = defaultdict(list)
    for (source, target), edge in relationships.items():
        outgoing[source].append((target, edge))
    file_map = {file["id"]: file for file in files}
    steps, visited = [], set()
    current, via = root, None
    while current and current["id"] not in visited and len(steps) < 8:
        visited.add(current["id"])
        options = [(target, edge) for target, edge in outgoing[current["id"]] if target not in visited]
        options.sort(key=lambda pair: (len(outgoing[pair[0]]) == 0, by_id[pair[0]].get("path", "")))
        steps.append({"node": current, "relationship": via["kind"] if via else None,
                      "citation": citation(analysis, file_map, via) if via else None,
                      "branches": [by_id[target] for target, _ in options[:20]]})
        current, via = (by_id[options[0][0]], options[0][1]) if options else (None, None)
    return {"starts": starts[:30], "steps": steps,
            "warnings": ["This is a path through recorded file dependencies, not an execution trace. Choose another branch to explore a different path."]}
