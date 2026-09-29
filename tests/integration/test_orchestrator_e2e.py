import asyncio
import time
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock
from openai import RateLimitError

from warden_orchestrator.models import Passage, CachedAnswer
from warden_orchestrator.api import create_app
from warden_orchestrator.cache import TwoTierCacheCoordinator
from warden_orchestrator.compressor import ContextCompressor
from warden_orchestrator.hyde import HyDEExpander
from warden_orchestrator.streaming import SSEStreamGenerator, DEGRADED_SERVICE_NOTICE

@pytest.fixture
def integrated_app():
    """Build application instance with wired components and controllable mocks."""
    cache = TwoTierCacheCoordinator(l1_capacity=100, l1_ttl_sec=60, redis_client=None)
    hyde = HyDEExpander()
    compressor = ContextCompressor()
    sse = SSEStreamGenerator()

    mock_router = AsyncMock()
    mock_router.classify_intent = AsyncMock(
        return_value=("HR_POLICY_QUESTION", {"HR_POLICY_QUESTION": 0.95})
    )

    mock_retrieval = AsyncMock()
    mock_retrieval.retrieve = AsyncMock(return_value=[
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=4,
            content="Employees with 12 months service receive 12 weeks of paid parental leave.",
            source_url="file:///data/policies/pto_policy_2026.md",
            role_tags=["Employee", "Manager"],
            rrf_score=0.04,
            calibrated_score=0.95,
        ),
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=5,
            content="Parental leave must be utilized within the first 12 months following birth or adoption.",
            source_url="file:///data/policies/pto_policy_2026.md",
            role_tags=["Employee", "Manager"],
            rrf_score=0.03,
            calibrated_score=0.89,
        ),
    ])

    mock_llm = MagicMock()
    mock_llm.generate_answer = AsyncMock(return_value=(
        "Full-time employees receive 12 weeks of paid parental leave [Doc: DOC-HR-LEAVE-2026, Chunk: 4].",
        175.0,
        18,
    ))

    async def mock_token_stream(query, context, caller_role="Employee", **kwargs):
        yield "Full-time"
        yield " employees"
        yield " receive"
        yield " 12 weeks."

    mock_llm.generate_stream = mock_token_stream

    app = create_app(
        cache_coordinator=cache,
        router=mock_router,
        retrieval_client=mock_retrieval,
        hyde_expander=hyde,
        compressor=compressor,
        llm_client=mock_llm,
        sse_generator=sse,
    )
    return app, mock_llm, mock_retrieval

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.cache
@pytest.mark.streaming
async def test_e2e_query_cache_hit_under_10ms(integrated_app):
    app, mock_llm, mock_retrieval = integrated_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        query = "How many weeks of parental leave do I receive?"

        # 1. Cold Query (Cache Miss)
        r1 = await ac.post("/query", json={"query": query}, headers={"X-User-Role": "Employee"})
        assert r1.status_code == 200
        data1 = r1.json()
        assert data1["metrics"]["cache_hit"] is False
        assert data1["metrics"]["prompt_cache_hit"] is True
        assert data1["metrics"]["compressed_context_tokens"] <= 800
        assert len(data1["citations"]) == 2
        assert mock_retrieval.retrieve.call_count == 1

        # 2. Warm Query (Cache Hit)
        start = time.perf_counter()
        r2 = await ac.post("/query", json={"query": query}, headers={"X-User-Role": "Employee"})
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["metrics"]["cache_hit"] is True
        assert data2["answer"] == data1["answer"]
        assert elapsed_ms < 10.0
        # Retrieval should not be called again
        assert mock_retrieval.retrieve.call_count == 1

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.cache
@pytest.mark.streaming
async def test_e2e_streaming_fallback_on_azure_openai_429(integrated_app):
    app, mock_llm, mock_retrieval = integrated_app

    # Simulate Azure OpenAI throwing 429
    err_response = MagicMock()
    err_response.status_code = 429
    err_response.headers = {}
    mock_llm.generate_stream = MagicMock(side_effect=RateLimitError("429 Throttled", response=err_response, body=None))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/query/stream",
            json={"query": "Parental leave policies"},
            headers={"X-User-Role": "Employee"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text

        # Verify degraded fallback stream contract
        assert "event: metadata" in text
        assert '"degraded": true' in text
        assert DEGRADED_SERVICE_NOTICE in text
        assert "event: citations" in text
        assert "DOC-HR-LEAVE-2026" in text
        assert "event: done\ndata: [DONE]\n\n" in text

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.cache
@pytest.mark.streaming
async def test_e2e_streaming_success_with_tokens(integrated_app):
    app, mock_llm, mock_retrieval = integrated_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/query/stream",
            json={"query": "Parental leave duration"},
            headers={"X-User-Role": "Employee"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text

        assert "event: metadata" in text
        assert '"degraded": false' in text
        assert "event: token" in text
        assert "Full-time" in text
        assert "event: citations" in text
        assert "event: done\ndata: [DONE]\n\n" in text

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.cache
@pytest.mark.streaming
async def test_e2e_role_partitioned_cache_isolation(integrated_app):
    app, mock_llm, mock_retrieval = integrated_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        query = "Identical query text for role partition test"

        # Employee query
        r_emp = await ac.post("/query", json={"query": query}, headers={"X-User-Role": "Employee"})
        assert r_emp.status_code == 200
        assert r_emp.json()["metrics"]["cache_hit"] is False
        assert mock_retrieval.retrieve.call_count == 1

        # Manager query with identical text -> must be a cache miss due to role partitioning
        r_mgr = await ac.post("/query", json={"query": query}, headers={"X-User-Role": "Manager"})
        assert r_mgr.status_code == 200
        assert r_mgr.json()["metrics"]["cache_hit"] is False
        assert mock_retrieval.retrieve.call_count == 2
