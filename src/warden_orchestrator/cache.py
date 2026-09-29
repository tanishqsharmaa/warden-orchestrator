"""Two-tier cache coordinator for warden-orchestrator."""

import asyncio
import json
import logging
import math
import random
import time
from collections import OrderedDict
from typing import Any

from warden_shared.cache import format_query_cache_key

from warden_orchestrator.models import CachedAnswer

logger = logging.getLogger("warden.orchestrator.cache")

# Atomic release Lua script
UNLOCK_LUA_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class TwoTierCacheCoordinator:
    """Coordinates L1 In-Memory LRU Cache and L2 Redis Sentinel Cache."""

    def __init__(
        self,
        l1_capacity: int = 1000,
        l1_ttl_sec: float = 60.0,
        l2_ttl_sec: int = 1800,
        redis_client: Any = None,
        beta: float = 1.0,
    ) -> None:
        self.l1_capacity = l1_capacity
        self.l1_ttl_sec = l1_ttl_sec
        self.l2_ttl_sec = l2_ttl_sec
        self.redis_client = redis_client
        self.beta = beta

        # Key -> (CachedAnswer, expire_epoch, role)
        self._l1_cache: OrderedDict[str, tuple[CachedAnswer, float, str]] = OrderedDict()
        self._l1_lock = asyncio.Lock()

    def _get_channel_key(self, role: str, query: str) -> str:
        cache_key = format_query_cache_key(role, query)
        return cache_key.replace("cache:query:", "channel:query:")

    def _get_lock_key(self, role: str, query: str) -> str:
        cache_key = format_query_cache_key(role, query)
        return cache_key.replace("cache:query:", "lock:query:")

    async def get(self, role: str, query: str) -> tuple[CachedAnswer | None, str | None]:
        """Fetch answer from L1 or L2 cache."""
        cache_key = format_query_cache_key(role, query)
        now = time.time()

        # 1. Check L1 In-Memory Cache
        async with self._l1_lock:
            if cache_key in self._l1_cache:
                ans, expiry, _ = self._l1_cache[cache_key]
                if now <= expiry:
                    self._l1_cache.move_to_end(cache_key)
                    return ans, "L1"
                # Expired in L1
                del self._l1_cache[cache_key]

        # 2. Check L2 Redis Cache
        if self.redis_client is None:
            return None, None

        try:
            raw = await self.redis_client.get(cache_key)
            if not raw:
                return None, None

            data = json.loads(raw)
            cached_obj = CachedAnswer.from_dict(data)

            # XFetch check: Delta = -beta * delta_t * ln(U)
            u = max(random.random(), 1e-10)
            delta = -self.beta * cached_obj.delta_t * math.log(u)
            expiry = data.get("expiry", cached_obj.created_at + cached_obj.ttl)

            # Store in L1 for fast immediate reads
            async with self._l1_lock:
                if len(self._l1_cache) >= self.l1_capacity:
                    self._l1_cache.popitem(last=False)
                self._l1_cache[cache_key] = (cached_obj, now + self.l1_ttl_sec, role)

            if (now - delta) > expiry:
                return cached_obj, "L2_REFRESH"
            return cached_obj, "L2"
        except Exception as exc:
            logger.warning("Redis L2 cache read error: %s (failing open)", exc)
            return None, None

    async def set(
        self,
        role: str,
        query: str,
        answer: str,
        citations: list[dict[str, Any]],
        delta_t: float,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Populate L1 and L2 cache entries."""
        cache_key = format_query_cache_key(role, query)
        now = time.time()
        cached_obj = CachedAnswer(
            answer=answer,
            citations=citations,
            metrics=metrics or {},
            created_at=now,
            delta_t=delta_t,
            ttl=self.l2_ttl_sec,
        )

        # 1. Populate L1 Cache
        async with self._l1_lock:
            if len(self._l1_cache) >= self.l1_capacity:
                self._l1_cache.popitem(last=False)
            self._l1_cache[cache_key] = (cached_obj, now + self.l1_ttl_sec, role)

        # 2. Populate L2 Redis Cache
        if self.redis_client is not None:
            try:
                envelope = cached_obj.to_dict()
                await self.redis_client.set(
                    cache_key,
                    json.dumps(envelope),
                    ex=self.l2_ttl_sec,
                )
            except Exception as exc:
                logger.warning("Redis L2 cache write error: %s", exc)

    async def acquire_singleflight(self, role: str, query: str, worker_uuid: str) -> bool:
        """Acquire distributed mutex lock for un-cached query computation."""
        if self.redis_client is None:
            return True

        lock_key = self._get_lock_key(role, query)
        try:
            acquired = await self.redis_client.set(lock_key, worker_uuid, px=5000, nx=True)
            return bool(acquired)
        except Exception as exc:
            logger.warning("Redis SingleFlight lock acquire error: %s", exc)
            return True

    async def release_singleflight(
        self,
        role: str,
        query: str,
        worker_uuid: str,
        answer_obj: CachedAnswer | None = None,
    ) -> None:
        """Release SingleFlight lock and broadcast result to waiting pods."""
        if self.redis_client is None:
            return

        channel = self._get_channel_key(role, query)
        lock_key = self._get_lock_key(role, query)

        try:
            if answer_obj is not None:
                payload = json.dumps(answer_obj.to_dict())
                await self.redis_client.publish(channel, payload)

            # Atomic release using Lua script
            if hasattr(self.redis_client, "eval"):
                await self.redis_client.eval(UNLOCK_LUA_SCRIPT, 1, lock_key, worker_uuid)
            else:
                current = await self.redis_client.get(lock_key)
                if current == worker_uuid:
                    await self.redis_client.delete(lock_key)
        except Exception as exc:
            logger.warning("Redis SingleFlight release error: %s", exc)

    async def wait_for_singleflight(
        self,
        role: str,
        query: str,
        timeout: float = 3.5,
    ) -> CachedAnswer | None:
        """Wait for concurrent worker to finish computing query via Pub/Sub."""
        if self.redis_client is None:
            return None

        channel_key = self._get_channel_key(role, query)
        cache_key = format_query_cache_key(role, query)

        try:
            pubsub = self.redis_client.pubsub()
            await pubsub.subscribe(channel_key)
            start = time.time()

            while (time.time() - start) < timeout:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if message and message.get("type") == "message":
                    raw = message["data"]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    data = json.loads(raw)
                    ans = CachedAnswer.from_dict(data)

                    # Populate into local L1
                    async with self._l1_lock:
                        if len(self._l1_cache) >= self.l1_capacity:
                            self._l1_cache.popitem(last=False)
                        self._l1_cache[cache_key] = (ans, time.time() + self.l1_ttl_sec, role)

                    await pubsub.unsubscribe(channel_key)
                    return ans
                await asyncio.sleep(0.05)

            await pubsub.unsubscribe(channel_key)
            # Final check in L2 before giving up
            final_ans, _ = await self.get(role, query)
            return final_ans
        except Exception as exc:
            logger.warning("Error waiting for SingleFlight notification: %s", exc)
            return None

    def flush_l1_role(self, role: str) -> int:
        """Flush all in-memory L1 entries matching the specified role."""
        flushed = 0
        keys_to_remove = [k for k, (_, _, r) in self._l1_cache.items() if r == role]
        for k in keys_to_remove:
            del self._l1_cache[k]
            flushed += 1
        return flushed
