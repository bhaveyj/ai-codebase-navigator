"""Public response schemas. Internal leases, cache inputs and DB keys stay private."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Edge, Node, SourceRange


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Repository(PublicModel):
    id: str
    owner: str
    name: str
    url: str
    defaultBranch: str
    latestAnalysisId: str | None = None
    createdAt: str


class Counts(PublicModel):
    files: int
    symbols: int
    edges: int
    chunks: int


class Citation(PublicModel):
    id: str
    analysisId: str
    fileId: str
    path: str
    commitSha: str
    startLine: int = Field(ge=1)
    endLine: int = Field(ge=1)
    chunkId: str | None = None
    label: str | None = None


class AnswerBlock(PublicModel):
    text: str
    kind: Literal["fact", "inference", "unknown"]
    citationIds: list[str]


class ChatResponse(PublicModel):
    blocks: list[AnswerBlock]
    citations: list[Citation]
    relatedNodeIds: list[str]
    mode: Literal["rag", "graph"]
    warnings: list[str]


class Overview(PublicModel):
    frameworks: list[str]
    entryPoints: list[str]
    routes: list[dict]
    integrations: list[str]
    summary: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    blocks: list[AnswerBlock] = Field(default_factory=list)


class Analysis(PublicModel):
    id: str
    repositoryId: str
    repositoryName: str
    commitSha: str
    status: Literal["queued", "running", "ready", "failed", "cancelled"]
    phase: str
    graphReady: bool
    ragReady: bool
    jobId: str
    counts: Counts
    languages: list[str]
    createdAt: str
    warnings: list[str]
    overview: Overview | None = None
    error: str | None = None


class Job(PublicModel):
    id: str
    analysisId: str
    status: Literal["queued", "running", "ready", "failed", "cancelled"]
    phase: str
    completed: int
    total: int
    message: str | None = None
    error: str | None = None
    cancelRequested: bool = False
    nextAttemptAt: str | None = None


class Submission(PublicModel):
    repository: Repository
    analysis: Analysis
    job: Job


class RepositoryList(PublicModel):
    repositories: list[Repository]
    analyses: list[Analysis]


class CodeNode(Node):
    model_config = ConfigDict(extra="ignore")


class CodeEdge(Edge):
    model_config = ConfigDict(extra="ignore")
    count: int | None = None
    edgeIds: list[str] | None = None


class GraphData(PublicModel):
    nodes: list[CodeNode]
    edges: list[CodeEdge]
    omittedNodes: int
    omittedEdges: int


class SourceFile(PublicModel):
    id: str
    analysisId: str
    path: str
    language: str
    content: str
    lineCount: int
    redacted: bool = False


class TreeEntry(PublicModel):
    id: str
    path: str
    name: str
    kind: Literal["file", "directory"]
    language: str | None = None
    childCount: int | None = None


class TreeResponse(PublicModel):
    entries: list[TreeEntry]


class SearchResult(PublicModel):
    id: str
    kind: str
    name: str
    path: str
    startLine: int | None = None
    symbolKind: str | None = None


class SearchResponse(PublicModel):
    results: list[SearchResult]
    total: int


class LanguageCapability(PublicModel):
    id: str
    label: str
    status: Literal["supported", "planned"]
    capabilities: list[str]


class Services(PublicModel):
    database: bool
    gemini: bool
    queue: bool


class Capabilities(PublicModel):
    languages: list[LanguageCapability]
    services: Services
    mode: Literal["local", "production"]
