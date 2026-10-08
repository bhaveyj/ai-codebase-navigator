from typing import Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRange(Contract):
    startLine: int = Field(ge=1)
    endLine: int = Field(ge=1)


class Node(Contract):
    id: str
    kind: Literal["repository", "module", "file", "symbol", "external"]
    name: str
    path: str | None = None
    fileId: str | None = None
    parentId: str | None = None
    language: str | None = None
    symbolKind: str | None = None
    startLine: int | None = None
    endLine: int | None = None
    startByte: int | None = None
    endByte: int | None = None
    signature: str | None = None
    exported: bool = False
    tags: list[str] = Field(default_factory=list)


class Edge(Contract):
    id: str
    source: str
    target: str
    kind: Literal["contains", "imports", "reexports", "references", "calls"]
    fileId: str | None = None
    startLine: int | None = None
    endLine: int | None = None
    specifier: str | None = None
    resolution: Literal["structural", "resolved"]
    typeOnly: bool = False
    dynamic: bool = False


class Region(SourceRange):
    fileId: str
    symbolId: str | None = None
    kind: str
    startByte: int = Field(ge=0)
    endByte: int = Field(ge=0)
    signature: str | None = None


class SubmitRequest(Contract):
    url: str = Field(min_length=1, max_length=2048)


class JobRetryRequest(Contract):
    retryAt: AwareDatetime | None = None


class HistoryTurn(Contract):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(Contract):
    message: str = Field(default="", max_length=4000)
    selectedNodeId: str | None = Field(default=None, max_length=2048)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)
    action: Literal["explain", "dependencies", "dependents"] | None = None


class GeneratedBlock(Contract):
    text: str = Field(max_length=12000)
    kind: Literal["fact", "inference", "unknown"]
    citationIds: list[str]
    edgeIds: list[str] = Field(default_factory=list)


class GeneratedAnswer(Contract):
    blocks: list[GeneratedBlock] = Field(min_length=1, max_length=20)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, *, retry_after_seconds: float | None = None, retryable: bool = True):
        self.code, self.message, self.status = code, message, status
        self.retry_after_seconds, self.retryable = retry_after_seconds, retryable
        super().__init__(message)
