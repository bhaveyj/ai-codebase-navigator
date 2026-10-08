from collections import deque

from .contracts import DomainError


def scoped_node(nodes, node_id):
    node = next((node for node in nodes if node["id"] == node_id), None)
    if not node:
        raise DomainError("NODE_NOT_FOUND", "The selected node does not belong to this analysis.", 404)
    return node


def neighbors(nodes, edges, focus, direction="both", depth=1, limit=100):
    scoped_node(nodes, focus)
    by_id = {node["id"]: node for node in nodes}
    adjacency = {}
    for edge in edges:
        if edge["kind"] == "contains":
            continue
        if direction in {"both", "dependencies"}:
            adjacency.setdefault(edge["source"], []).append((edge["target"], edge))
        if direction in {"both", "dependents"}:
            adjacency.setdefault(edge["target"], []).append((edge["source"], edge))
    seen, selected_edges, queue = {focus}, {}, deque([(focus, 0)])
    omitted = set()
    while queue:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        for target, edge in adjacency.get(current, []):
            if target not in by_id:
                continue
            if target not in seen and len(seen) >= limit:
                omitted.add(target)
                continue
            selected_edges[edge["id"]] = edge
            if target not in seen:
                seen.add(target)
                queue.append((target, distance + 1))
    return [by_id[id] for id in sorted(seen)], list(selected_edges.values()), len(omitted)


def project_graph(nodes, edges, level="modules", focus=None, direction="both", depth=1, language=None, kind=None):
    if focus:
        focus_node = scoped_node(nodes, focus)
        # A container focus drills into its children; files/symbols explore actual dependencies.
        if focus_node["kind"] in {"repository", "module"}:
            candidates = [node for node in nodes if node.get("parentId") == focus or node["id"] == focus]
            if level == "files":
                directory = focus_node.get("path", "").rstrip("/")
                prefix = "" if directory in {"", "."} else directory + "/"
                candidates = [node for node in nodes if node["kind"] == "file" and (not prefix or node.get("path", "").startswith(prefix))]
            omitted_neighbors = 0
        elif level == "symbols" and focus_node["kind"] == "file":
            candidates = [node for node in nodes if node.get("fileId") == focus or node["id"] == focus]
            omitted_neighbors = 0
        else:
            candidates, _, omitted_neighbors = neighbors(nodes, edges, focus, direction, depth, 1000)
    else:
        allowed = {"repository", "module"} if level == "modules" else {"file"} if level == "files" else {"symbol"}
        candidates = [node for node in nodes if node["kind"] in allowed]
        if level == "modules":
            # Start with broad directory boundaries; drill/search exposes deeper folders.
            candidates = [node for node in candidates if node["kind"] == "repository" or len([part for part in node.get("path", "").split("/") if part not in {"", "."}]) <= 2]
        omitted_neighbors = 0
    if language:
        candidates = [node for node in candidates if node.get("language") == language or node["kind"] in {"repository", "module"}]
    if kind:
        candidates = [node for node in candidates if node["kind"] == kind or node.get("symbolKind") == kind]
    candidates.sort(key=lambda n: (n["kind"] != "repository", n.get("path", ""), n["name"]))
    selected = candidates[:100]
    ids = {node["id"] for node in selected}
    selected_edges = [edge for edge in edges if edge["source"] in ids and edge["target"] in ids]
    # Project file imports onto their module ancestors, retaining edge IDs as provenance.
    if level == "modules":
        by_id = {node["id"]: node for node in nodes}
        def ancestor(node_id):
            visited = set()
            while node_id in by_id and node_id not in visited:
                visited.add(node_id)
                node = by_id[node_id]
                if node["kind"] == "module" and node_id in ids:
                    return node_id
                node_id = node.get("parentId") or node.get("fileId")
            return None
        grouped = {}
        for edge in edges:
            if edge["kind"] not in {"imports", "reexports"}:
                continue
            source, target = ancestor(edge["source"]), ancestor(edge["target"])
            if source and target and source != target:
                key = (source, target, edge["kind"])
                if key not in grouped:
                    grouped[key] = {"id": f"aggregate:{source}:{target}:{edge['kind']}", "source": source, "target": target, "kind": edge["kind"], "resolution": "resolved", "count": 0, "edgeIds": []}
                grouped[key]["count"] += 1
                grouped[key]["edgeIds"].append(edge["id"])
        selected_edges += list(grouped.values())
    if focus and scoped_node(nodes, focus)["kind"] in {"file", "symbol"} and level != "symbols":
        selected_edges = [edge for edge in selected_edges if edge["kind"] != "contains"]
    return {"nodes": selected, "edges": selected_edges[:200], "omittedNodes": max(0, len(candidates) - 100) + omitted_neighbors, "omittedEdges": max(0, len(selected_edges) - 200)}
