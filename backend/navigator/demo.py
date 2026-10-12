"""Questions answered once for the public, pre-indexed example repository."""
import hashlib

QUESTIONS = (
    "How is this project structured?",
    "How does authentication work?",
    "Where are the API routes?",
)


def answer_id(analysis_id: str, question: str) -> str:
    return f"{analysis_id}:{hashlib.sha256(question.strip().casefold().encode()).hexdigest()}"


def routes_answer(store, analysis):
    """Build the sample's routes answer from its verified static route map."""
    files = {file["path"]: file for file in store.find("files", {"analysisId": analysis["id"]})}
    blocks, citations, related = [], [], []
    for route in analysis.get("overview", {}).get("routes", []):
        path = route.get("filePath") or route.get("path")
        if not path or ("/routes/" not in path and not path.endswith("/app.ts")):
            continue
        file = files.get(path)
        if not file or not file.get("lineCount"):
            continue
        line = min(max(1, route.get("startLine") or 1), file["lineCount"])
        citation_id = f"D{len(citations) + 1}"
        citations.append({"id": citation_id, "analysisId": analysis["id"],
                          "fileId": file["id"], "path": path,
                          "commitSha": analysis["commitSha"], "startLine": line,
                          "endLine": line, "label": f"{path}:{line}"})
        blocks.append({"text": f"{route.get('method') or 'ROUTE'} {route.get('route') or '/'} is declared in {path}.",
                       "kind": "fact", "citationIds": [citation_id]})
        if route.get("nodeId"):
            related.append(route["nodeId"])
        if len(blocks) == 8:
            break
    if not blocks:
        blocks = [{"text": "No API routes were identified by the sample's static analysis.",
                   "kind": "unknown", "citationIds": []}]
    return {"blocks": blocks, "citations": citations, "relatedNodeIds": related,
            "mode": "graph", "warnings": ["Dynamic routes may not appear in the static map."]}
