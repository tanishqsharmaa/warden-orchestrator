"""Domain data models and schemas for warden-orchestrator."""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


@dataclass
class Passage:
    """Represents a retrieved and/or reranked document passage."""

    doc_id: str
    chunk_index: int
    content: str
    source_url: str
    role_tags: list[str] = field(default_factory=list)
    rrf_score: float = 0.0
    calibrated_score: float = 0.0


@dataclass
class Citation:
    """Citation metadata mapped to source documents."""

    citation_id: int
    doc_id: str
    chunk_index: int
    source_url: str
    quoted_snippet: str = ""


@dataclass
class CachedAnswer:
    """Envelope for two-tier cached query answers."""

    answer: str
    citations: list[dict[str, Any]]
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    delta_t: float = 0.0
    ttl: int = 1800

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": self.citations,
            "metrics": self.metrics,
            "created_at": self.created_at,
            "delta_t": self.delta_t,
            "ttl": self.ttl,
            "expiry": self.created_at + self.ttl,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CachedAnswer":
        return cls(
            answer=data["answer"],
            citations=data.get("citations", []),
            metrics=data.get("metrics", {}),
            created_at=data.get("created_at", 0.0),
            delta_t=data.get("delta_t", 0.0),
            ttl=data.get("ttl", 1800),
        )


class QueryRequest(BaseModel):
    """Client request for query execution."""

    query: str = Field(..., min_length=1, description="Policy question or search prompt")
    stream: bool = Field(default=False, description="Whether to stream response over SSE")


class QueryMetrics(BaseModel):
    """Execution performance metrics."""

    total_latency_ms: float = 0.0
    cache_hit: bool = False
    cache_tier: str | None = None
    retrieval_latency_ms: float = 0.0
    compressed_context_tokens: int = 0
    llm_ttft_ms: float = 0.0
    tokens_generated: int = 0
    prompt_cache_hit: bool = False


class QueryResponse(BaseModel):
    """Synchronous JSON response for query execution."""

    query: str
    caller_role: str
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    metrics: QueryMetrics
    trace_id: str
