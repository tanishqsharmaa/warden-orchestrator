import pytest

from warden_orchestrator.models import CachedAnswer, Passage
from warden_orchestrator.streaming import DEGRADED_SERVICE_NOTICE, SSEStreamGenerator


@pytest.mark.asyncio
async def test_sse_event_sequence():
    async def mock_tokens():
        yield "Full-time"
        yield " employees"
        yield " receive"
        yield " 12 weeks."

    passages = [
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=4,
            content="Employees receive 12 weeks of parental leave.",
            source_url="file:///data/pto.md",
            rrf_score=0.04,
            calibrated_score=0.95,
        )
    ]

    generator = SSEStreamGenerator()
    chunks = [
        event async for event in generator.stream_events(
            trace_id="trace-7f93b5a1",
            caller_role="Employee",
            cache_hit=False,
            token_generator=mock_tokens(),
            passages=passages,
        )
    ]

    joined = "".join(chunks)
    assert joined.startswith("event: metadata\n")
    assert "event: token\n" in joined
    assert "event: citations\n" in joined
    assert joined.endswith("event: done\ndata: [DONE]\n\n")

    # Check metadata content
    assert '"trace_id": "trace-7f93b5a1"' in joined
    assert '"caller_role": "Employee"' in joined

    # Check citation structure
    assert '"citation_id": 1' in joined
    assert '"doc_id": "DOC-HR-LEAVE-2026"' in joined

@pytest.mark.asyncio
async def test_sse_degraded_service_stream():
    passages = [
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=2,
            content="Bereavement leave covers 5 days.",
            source_url="file:///data/pto.md",
            rrf_score=0.03,
            calibrated_score=0.91,
        )
    ]

    generator = SSEStreamGenerator()
    chunks = [
        event async for event in generator.stream_degraded(
            trace_id="trace-degraded",
            caller_role="Employee",
            passages=passages,
        )
    ]

    joined = "".join(chunks)
    assert "event: metadata\n" in joined
    assert '"degraded": true' in joined
    assert DEGRADED_SERVICE_NOTICE in joined
    assert "event: citations\n" in joined
    assert joined.endswith("event: done\ndata: [DONE]\n\n")

@pytest.mark.asyncio
async def test_sse_cached_answer_playback():
    cached = CachedAnswer(
        answer="18 days PTO.",
        citations=[{"citation_id": 1, "doc_id": "DOC-1", "chunk_index": 0, "source_url": "file:///pto.md"}],
        metrics={"cache_hit": True},
        created_at=1000.0,
        delta_t=0.1,
    )

    generator = SSEStreamGenerator()
    chunks = [
        event async for event in generator.stream_cached(
            trace_id="trace-cached",
            caller_role="Employee",
            cached_answer=cached,
        )
    ]

    joined = "".join(chunks)
    assert '"cache_hit": true' in joined
    assert "18 days PTO." in joined
    assert "event: citations\n" in joined
    assert joined.endswith("event: done\ndata: [DONE]\n\n")
