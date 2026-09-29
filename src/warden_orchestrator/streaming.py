"""Server-Sent Events (SSE) streaming generator and citation serializer."""

import json
from typing import AsyncGenerator
from warden_orchestrator.models import Passage, CachedAnswer

DEGRADED_SERVICE_NOTICE = (
    "Generative synthesis is temporarily unavailable due to upstream provider rate limits. "
    "Below are the verified policy passages matching your inquiry:"
)


class SSEStreamGenerator:
    """Generates RFC-compliant text/event-stream chunks."""

    def _format_event(self, event: str, data: str) -> str:
        """Format individual SSE event."""
        return f"event: {event}\ndata: {data}\n\n"

    def _build_citations(self, passages: list[Passage]) -> list[dict]:
        """Construct citation metadata array."""
        citations = []
        for i, p in enumerate(passages):
            citations.append({
                "citation_id": i + 1,
                "doc_id": p.doc_id,
                "chunk_index": p.chunk_index,
                "source_url": p.source_url,
            })
        return citations

    async def stream_events(
        self,
        trace_id: str,
        caller_role: str,
        cache_hit: bool,
        token_generator: AsyncGenerator[str, None],
        passages: list[Passage],
        is_degraded: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Stream metadata -> tokens -> citations -> done."""
        # 1. Metadata Event
        metadata = {
            "trace_id": trace_id,
            "caller_role": caller_role,
            "cache_hit": cache_hit,
            "degraded": is_degraded,
        }
        yield self._format_event("metadata", json.dumps(metadata))

        # 2. Token Events
        async for token in token_generator:
            yield self._format_event("token", json.dumps({"token": token}))

        # 3. Citations Event
        citations = self._build_citations(passages)
        yield self._format_event("citations", json.dumps(citations))

        # 4. Done Event
        yield self._format_event("done", "[DONE]")

    async def stream_degraded(
        self,
        trace_id: str,
        caller_role: str,
        passages: list[Passage],
        notice: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream degraded service response with verified citations."""
        # 1. Metadata Event
        metadata = {
            "trace_id": trace_id,
            "caller_role": caller_role,
            "cache_hit": False,
            "degraded": True,
        }
        yield self._format_event("metadata", json.dumps(metadata))

        # 2. Degraded Token Notice
        msg = notice or DEGRADED_SERVICE_NOTICE
        yield self._format_event("token", json.dumps({"token": msg}))

        for i, p in enumerate(passages):
            snippet = f"\n\n[Doc: {p.doc_id}, Chunk: {p.chunk_index}]\n{p.content}"
            yield self._format_event("token", json.dumps({"token": snippet}))

        # 3. Citations Event
        citations = self._build_citations(passages)
        yield self._format_event("citations", json.dumps(citations))

        # 4. Done Event
        yield self._format_event("done", "[DONE]")

    async def stream_cached(
        self,
        trace_id: str,
        caller_role: str,
        cached_answer: CachedAnswer,
    ) -> AsyncGenerator[str, None]:
        """Stream cached answer instantly with citation metadata."""
        metadata = {
            "trace_id": trace_id,
            "caller_role": caller_role,
            "cache_hit": True,
            "degraded": False,
        }
        yield self._format_event("metadata", json.dumps(metadata))
        yield self._format_event("token", json.dumps({"token": cached_answer.answer}))
        yield self._format_event("citations", json.dumps(cached_answer.citations))
        yield self._format_event("done", "[DONE]")
