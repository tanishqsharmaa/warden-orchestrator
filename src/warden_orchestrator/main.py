"""Main entrypoint and lifespan runner for warden-orchestrator."""

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from warden_shared.cache import RedisConnectionManager
from warden_shared.logging import configure_logging

from warden_orchestrator.config import get_settings
from warden_orchestrator.api import create_app
from warden_orchestrator.cache import TwoTierCacheCoordinator
from warden_orchestrator.retrieval_client import RetrievalClient
from warden_orchestrator.router import QueryIntentRouter

logger = logging.getLogger("warden.orchestrator")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown lifecycle manager."""
    settings = get_settings()
    configure_logging(service_name="warden-orchestrator", level="INFO")
    logger.info("Initializing warden-orchestrator service...")

    # Initialize Redis client via warden-shared connection manager
    redis_mgr = RedisConnectionManager(redis_url=settings.redis_url)
    try:
        redis_client = await redis_mgr.get_client()
        logger.info("Connected to Redis cache at %s", settings.redis_url)
    except Exception as exc:
        logger.warning("Could not establish immediate Redis connection: %s (failing open)", exc)
        redis_client = None

    # Initialize cache coordinator and gRPC clients
    cache_coord = TwoTierCacheCoordinator(
        l1_capacity=settings.l1_cache_capacity,
        l1_ttl_sec=settings.l1_cache_ttl_sec,
        l2_ttl_sec=settings.l2_cache_ttl_sec,
        redis_client=redis_client,
    )
    retrieval_client = RetrievalClient(grpc_target=settings.retrieval_grpc_url)
    router = QueryIntentRouter(grpc_target=settings.laya_grpc_url)

    app.state.cache = cache_coord
    app.state.retrieval_client = retrieval_client
    app.state.router = router
    app.state.redis_mgr = redis_mgr

    yield

    logger.info("Shutting down warden-orchestrator service...")
    await retrieval_client.close()
    await router.close()
    await redis_mgr.close()
    logger.info("Shutdown complete.")


def get_application() -> FastAPI:
    """Instantiate production FastAPI application."""
    settings = get_settings()
    app = create_app(settings=settings)
    app.router.lifespan_context = lifespan
    return app


app = get_application()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("warden_orchestrator.main:app", host="0.0.0.0", port=settings.port, reload=False)
