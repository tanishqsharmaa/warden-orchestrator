from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from warden_orchestrator.api import create_app
from warden_orchestrator.models import CachedAnswer, Passage


@pytest.fixture
def mock_deps():
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=(None, None))
    mock_cache.acquire_singleflight = AsyncMock(return_value=True)
    mock_cache.release_singleflight = AsyncMock()
    mock_cache.set = AsyncMock()
    mock_cache.redis_client = MagicMock()

    mock_router = AsyncMock()
    mock_router.classify_intent = AsyncMock(return_value=("HR_POLICY_QUESTION", {"HR_POLICY_QUESTION": 0.95}))

    mock_retrieval = AsyncMock()
    mock_retrieval.retrieve = AsyncMock(return_value=[
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=4,
            content="Employees receive 12 weeks of parental leave.",
            source_url="file:///data/pto.md",
            rrf_score=0.04,
            calibrated_score=0.95,
        )
    ])

    mock_llm = MagicMock()
    mock_llm.generate_answer = AsyncMock(return_value=(
        "Full-time employees receive 12 weeks of parental leave [Doc: DOC-HR-LEAVE-2026, Chunk: 4].",
        180.0,
        15,
    ))

    async def mock_stream(q, c, r):
        yield "Full-time employees receive 12 weeks."

    mock_llm.generate_stream = mock_stream

    return {
        "cache": mock_cache,
        "router": mock_router,
        "retrieval": mock_retrieval,
        "llm": mock_llm,
    }

@pytest.mark.asyncio
async def test_query_rejects_missing_role():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/query", json={"query": "PTO policy"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["error_code"] == "ERR_AUTH_ROLE_MISSING"

@pytest.mark.asyncio
async def test_query_rejects_invalid_role():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/query", json={"query": "PTO policy"}, headers={"X-User-Role": "Contractor"})
        assert resp.status_code == 403
        data = resp.json()
        assert data["error_code"] == "ERR_AUTH_ROLE_INVALID"

@pytest.mark.asyncio
async def test_stream_rejects_missing_role():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/query/stream", json={"query": "PTO policy"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["error_code"] == "ERR_AUTH_ROLE_MISSING"

@pytest.mark.asyncio
async def test_query_synchronous_success_cache_miss(mock_deps):
    app = create_app(
        cache_coordinator=mock_deps["cache"],
        router=mock_deps["router"],
        retrieval_client=mock_deps["retrieval"],
        llm_client=mock_deps["llm"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/query",
            json={"query": "How many weeks of parental leave?"},
            headers={"X-User-Role": "Employee"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["caller_role"] == "Employee"
        assert "12 weeks of parental leave" in data["answer"]
        assert len(data["citations"]) == 1
        assert data["citations"][0]["doc_id"] == "DOC-HR-LEAVE-2026"
        assert data["metrics"]["cache_hit"] is False

@pytest.mark.asyncio
async def test_query_synchronous_success_cache_hit(mock_deps):
    cached = CachedAnswer(
        answer="Cached answer: 12 weeks",
        citations=[{"doc_id": "DOC-HR-LEAVE-2026", "chunk_index": 4}],
        metrics={"cache_hit": True},
        created_at=100.0,
        delta_t=0.5,
    )
    mock_deps["cache"].get = AsyncMock(return_value=(cached, "L1"))

    app = create_app(
        cache_coordinator=mock_deps["cache"],
        router=mock_deps["router"],
        retrieval_client=mock_deps["retrieval"],
        llm_client=mock_deps["llm"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/query",
            json={"query": "Parental leave"},
            headers={"X-User-Role": "Employee"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "Cached answer: 12 weeks"
        assert data["metrics"]["cache_hit"] is True

@pytest.mark.asyncio
async def test_query_out_of_scope_early_rejection(mock_deps):
    mock_deps["router"].classify_intent = AsyncMock(
        return_value=("OUT_OF_SCOPE_REQUEST", {"OUT_OF_SCOPE_REQUEST": 0.98})
    )
    app = create_app(
        cache_coordinator=mock_deps["cache"],
        router=mock_deps["router"],
        retrieval_client=mock_deps["retrieval"],
        llm_client=mock_deps["llm"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/query",
            json={"query": "Help me write code for my freelancing app"},
            headers={"X-User-Role": "Employee"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "out of scope" in data["answer"].lower()
        mock_deps["retrieval"].retrieve.assert_not_called()

@pytest.mark.asyncio
async def test_query_stream_success(mock_deps):
    app = create_app(
        cache_coordinator=mock_deps["cache"],
        router=mock_deps["router"],
        retrieval_client=mock_deps["retrieval"],
        llm_client=mock_deps["llm"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/query/stream",
            json={"query": "Parental leave"},
            headers={"X-User-Role": "Employee"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text
        assert "event: metadata" in text
        assert "event: token" in text
        assert "event: citations" in text
        assert "event: done" in text

@pytest.mark.asyncio
async def test_health_endpoint():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "HEALTHY"
        assert data["service"] == "warden-orchestrator"
