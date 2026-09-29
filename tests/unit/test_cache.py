import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from warden_orchestrator.cache import TwoTierCacheCoordinator
from warden_orchestrator.models import CachedAnswer


@pytest.mark.asyncio
async def test_l1_cache_hit_under_2ms():
    coord = TwoTierCacheCoordinator(l1_capacity=10, l1_ttl_sec=60, redis_client=None)
    role = "Employee"
    query = "How many days of bereavement leave?"

    await coord.set(role, query, answer="5 days", citations=[{"doc_id": "DOC-1"}], delta_t=0.5)

    start = time.perf_counter()
    res, tier = await coord.get(role, query)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert tier == "L1"
    assert res is not None
    assert res.answer == "5 days"
    assert elapsed_ms < 2.0

@pytest.mark.asyncio
async def test_l1_cache_lru_eviction():
    coord = TwoTierCacheCoordinator(l1_capacity=2, l1_ttl_sec=60, redis_client=None)
    role = "Employee"

    await coord.set(role, "q1", "a1", [], delta_t=0.1)
    await coord.set(role, "q2", "a2", [], delta_t=0.1)
    await coord.set(role, "q3", "a3", [], delta_t=0.1)

    res1, _ = await coord.get(role, "q1")
    res2, _ = await coord.get(role, "q2")
    res3, _ = await coord.get(role, "q3")

    assert res1 is None  # Evicted
    assert res2 is not None
    assert res3 is not None

@pytest.mark.asyncio
async def test_l1_cache_ttl_expiration():
    coord = TwoTierCacheCoordinator(l1_capacity=10, l1_ttl_sec=0.01, redis_client=None)
    role = "Employee"

    await coord.set(role, "q1", "a1", [], delta_t=0.1)
    await asyncio.sleep(0.02)

    res, tier = await coord.get(role, "q1")
    assert res is None
    assert tier is None

@pytest.mark.asyncio
async def test_l2_cache_hit_and_l1_population():
    mock_redis = AsyncMock()
    now = time.time()
    envelope = {
        "answer": "12 weeks",
        "citations": [{"doc_id": "DOC-2"}],
        "metrics": {"prompt_cache_hit": True},
        "created_at": now,
        "delta_t": 0.4,
        "ttl": 1800,
        "expiry": now + 1800
    }
    mock_redis.get = AsyncMock(return_value=json.dumps(envelope))

    coord = TwoTierCacheCoordinator(l1_capacity=10, l1_ttl_sec=60, redis_client=mock_redis)
    role = "Employee"
    query = "Parental leave"

    res, tier = await coord.get(role, query)
    assert res is not None
    assert res.answer == "12 weeks"
    assert tier == "L2"

    # Now verify it was populated into L1
    res_l1, tier_l1 = await coord.get(role, query)
    assert tier_l1 == "L1"
    assert res_l1.answer == "12 weeks"

@pytest.mark.asyncio
async def test_singleflight_mutex_and_waiter_notification():
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=[True, False])
    mock_pubsub = AsyncMock()

    # Simulate waiter receiving message on pub/sub
    payload = json.dumps({
        "answer": "Computed answer",
        "citations": [],
        "metrics": {},
        "created_at": time.time(),
        "delta_t": 0.5,
        "ttl": 1800,
        "expiry": time.time() + 1800
    })
    mock_pubsub.get_message = AsyncMock(side_effect=[
        None,
        {"type": "message", "data": payload}
    ])
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
    mock_redis.publish = AsyncMock()
    mock_redis.eval = AsyncMock()

    coord = TwoTierCacheCoordinator(l1_capacity=10, l1_ttl_sec=60, redis_client=mock_redis)
    role = "Employee"
    query = "Concurrent query"

    won = await coord.acquire_singleflight(role, query, worker_uuid="worker-1")
    assert won is True

    contender = await coord.acquire_singleflight(role, query, worker_uuid="worker-2")
    assert contender is False

    # Contender waits for result
    wait_task = asyncio.create_task(coord.wait_for_singleflight(role, query, timeout=2.0))
    await asyncio.sleep(0.01)

    # Winner releases lock and notifies
    answer_obj = CachedAnswer(
        answer="Computed answer",
        citations=[],
        metrics={},
        created_at=time.time(),
        delta_t=0.5,
        ttl=1800
    )
    await coord.release_singleflight(role, query, worker_uuid="worker-1", answer_obj=answer_obj)

    result = await wait_task
    assert result is not None
    assert result.answer == "Computed answer"

@pytest.mark.asyncio
async def test_flush_l1_role():
    coord = TwoTierCacheCoordinator(l1_capacity=10, l1_ttl_sec=60, redis_client=None)
    await coord.set("Employee", "q1", "a1", [], delta_t=0.1)
    await coord.set("Manager", "q1", "a1_mgr", [], delta_t=0.1)

    flushed = coord.flush_l1_role("Employee")
    assert flushed == 1

    res_emp, _ = await coord.get("Employee", "q1")
    res_mgr, _ = await coord.get("Manager", "q1")
    assert res_emp is None
    assert res_mgr is not None
