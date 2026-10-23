from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CandidateKind(StrEnum):
    MCP_SERVER = "mcp_server"
    PYPI_PACKAGE = "pypi_package"


class ToolStatus(StrEnum):
    DISCOVERED = "discovered"  # metadata only, nothing downloaded or executed
    QUARANTINED = "quarantined"  # under review / sandbox testing
    APPROVED = "approved"  # owner-approved, version pinned
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class Provenance(BaseModel):
    source: str  # endpoint queried
    fetched_at: datetime
    response_sha256: str


class Candidate(BaseModel):
    kind: CandidateKind
    name: str
    version: str | None = None
    description: str = ""
    repository_url: str | None = None
    homepage: str | None = None
    license: str | None = None
    last_updated: datetime | None = None
    transports: list[str] = []  # MCP: stdio / streamable-http / sse
    is_remote: bool = False  # MCP: hosted endpoint (our data leaves the machine)
    requires_python: str | None = None
    platforms: list[str] = []  # wheel platform tags
    provenance: Provenance


class Score(BaseModel):
    relevance: float = Field(ge=0, le=5)
    security: float = Field(ge=0, le=5)
    maintenance: float = Field(ge=0, le=5)
    compatibility: float = Field(ge=0, le=5)
    license: float = Field(ge=0, le=5)
    cost: float = Field(ge=0, le=5)
    permissions: float = Field(ge=0, le=5)
    total: float = Field(ge=0, le=5)
    flags: list[str] = []  # human-readable concerns; any flag => manual review


class ScoredCandidate(BaseModel):
    candidate: Candidate
    score: Score
