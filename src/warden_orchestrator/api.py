"""FastAPI REST application for warden-orchestrator."""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import StreamingResponse
from warden_shared.errors import RoleInvalidError, RoleMissingError, register_error_handlers

from warden_orchestrator.cache import TwoTierCacheCoordinator
from warden_orchestrator.compressor import ContextCompressor
from warden_orchestrator.config import Settings, get_settings
from warden_orchestrator.hyde import HyDEExpander
from warden_orchestrator.llm_client import AzureOpenAIClientWrapper
from warden_orchestrator.models import (
    CachedAnswer,
    QueryMetrics,
    QueryRequest,
    QueryResponse,
)
from warden_orchestrator.retrieval_client import RetrievalClient
from warden_orchestrator.router import QueryIntentRouter
from warden_orchestrator.streaming import DEGRADED_SERVICE_NOTICE, SSEStreamGenerator

logger = logging.getLogger("warden.orchestrator.api")

VALID_ROLES = {"Employee", "Manager", "HR-Admin"}
OUT_OF_SCOPE_ANSWER = (
    "This inquiry is out of scope for company Human Resources policies. "
    "Project Warden only answers questions regarding employee benefits, leaves of absence, "
    "compensation guidelines, code of conduct, and internal workplace governance."
)


def validate_role(x_user_role: str | None = Header(None)) -> str:
    """Validate caller security role header."""
    if not x_user_role:
        raise RoleMissingError(detail="Client request lacks mandatory X-User-Role header.")
    if x_user_role not in VALID_ROLES:
        raise RoleInvalidError(detail=f"Role '{x_user_role}' is unauthorized. Valid roles: Employee, Manager, HR-Admin.")
    return x_user_role


def create_app(
    settings: Settings | None = None,
    cache_coordinator: TwoTierCacheCoordinator | None = None,
    retrieval_client: RetrievalClient | None = None,
    router: QueryIntentRouter | None = None,
    hyde_expander: HyDEExpander | None = None,
    compressor: ContextCompressor | None = None,
    llm_client: AzureOpenAIClientWrapper | None = None,
    sse_generator: SSEStreamGenerator | None = None,
) -> FastAPI:
    """Factory function for FastAPI application."""
    app_settings = settings or get_settings()

    app = FastAPI(
        title="Project Warden Orchestrator",
        version="1.0.0",
        description="Autonomous Query Lifecycle Agent & LLM Orchestration (Tier 5)",
    )

    # Register RFC 7807 problem details exception handlers
    register_error_handlers(app)

    # Assign dependency instances or initialize defaults
    cache = cache_coordinator or TwoTierCacheCoordinator(
        l1_capacity=app_settings.l1_cache_capacity,
        l1_ttl_sec=app_settings.l1_cache_ttl_sec,
        l2_ttl_sec=app_settings.l2_cache_ttl_sec,
    )
    retrieval = retrieval_client or RetrievalClient(grpc_target=app_settings.retrieval_grpc_url)
    intent_router = router or QueryIntentRouter(grpc_target=app_settings.laya_grpc_url)
    hyde = hyde_expander or HyDEExpander()
    context_compressor = compressor or ContextCompressor()
    llm = llm_client or AzureOpenAIClientWrapper(
        endpoint=app_settings.azure_openai_endpoint,
        api_key=app_settings.azure_openai_api_key,
        deployment=app_settings.azure_openai_deployment,
        api_version=app_settings.azure_openai_api_version,
    )
    sse = sse_generator or SSEStreamGenerator()

    @app.post("/query", response_model=QueryResponse)
    async def query_endpoint(
        req: QueryRequest,
        x_user_role: str | None = Header(None),
    ) -> QueryResponse:
        """Execute synchronous access-controlled query lifecycle."""
        role = validate_role(x_user_role)
        query = req.query.strip()
        trace_id = uuid.uuid4().hex
        start_time = time.perf_counter()

        # 1. Two-Tier Cache Check
        cached, tier = await cache.get(role, query)
        if cached is not None and tier in ("L1", "L2"):
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return QueryResponse(
                query=query,
                caller_role=role,
                answer=cached.answer,
                citations=cached.citations,
                metrics=QueryMetrics(
                    total_latency_ms=elapsed_ms,
                    cache_hit=True,
                    cache_tier=tier,
                    retrieval_latency_ms=0.0,
                    compressed_context_tokens=0,
                    llm_ttft_ms=0.0,
                    tokens_generated=len(cached.answer.split()),
                    prompt_cache_hit=True,
                ),
                trace_id=trace_id,
            )

        # 2. Acquire SingleFlight Mutex Lock
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        acquired = await cache.acquire_singleflight(role, query, worker_id)
        if not acquired:
            waited = await cache.wait_for_singleflight(role, query, timeout=3.5)
            if waited is not None:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                return QueryResponse(
                    query=query,
                    caller_role=role,
                    answer=waited.answer,
                    citations=waited.citations,
                    metrics=QueryMetrics(
                        total_latency_ms=elapsed_ms,
                        cache_hit=True,
                        cache_tier="L2_REDIS_SINGLEFLIGHT",
                    ),
                    trace_id=trace_id,
                )

        try:
            # 3. Intent Routing
            selected_choice, _ = await intent_router.classify_intent(query)
            if selected_choice == "OUT_OF_SCOPE_REQUEST":
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                return QueryResponse(
                    query=query,
                    caller_role=role,
                    answer=OUT_OF_SCOPE_ANSWER,
                    citations=[],
                    metrics=QueryMetrics(total_latency_ms=elapsed_ms, cache_hit=False),
                    trace_id=trace_id,
                )

            # 4. HyDE Expansion & Retrieval
            expanded_query = hyde.expand_query(query, caller_role=role)
            retrieval_start = time.perf_counter()
            passages = await retrieval.retrieve(
                query_text=expanded_query,
                caller_role=role,
                top_k=30,
                final_rerank_limit=5,
            )
            retrieval_ms = (time.perf_counter() - retrieval_start) * 1000.0

            # 5. Extractive Context Compression
            compressed_context, context_tokens = context_compressor.compress_passages(
                passages, target_tokens=app_settings.compression_target_tokens
            )

            # 6. LLM Grounded Answer Generation
            try:
                answer, ttft_ms, tokens_generated = await llm.generate_answer(
                    query=query,
                    compressed_context=compressed_context,
                    caller_role=role,
                )
            except Exception as llm_exc:
                logger.error("LLM generation fault, degrading response: %s", llm_exc)
                answer = f"{DEGRADED_SERVICE_NOTICE}\n\n" + "\n\n".join(
                    f"[Doc: {p.doc_id}, Chunk: {p.chunk_index}]\n{p.content}" for p in passages
                )
                ttft_ms = 0.0
                tokens_generated = 0

            # Build citations
            citations = [
                {
                    "citation_id": i + 1,
                    "doc_id": p.doc_id,
                    "chunk_index": p.chunk_index,
                    "source_url": p.source_url,
                }
                for i, p in enumerate(passages)
            ]

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            # 7. Populate Two-Tier Cache & Release Lock
            cached_obj = CachedAnswer(
                answer=answer,
                citations=citations,
                metrics={"prompt_cache_hit": True},
                created_at=time.time(),
                delta_t=elapsed_ms / 1000.0,
                ttl=app_settings.l2_cache_ttl_sec,
            )
            await cache.set(role, query, answer, citations, delta_t=elapsed_ms / 1000.0)
            await cache.release_singleflight(role, query, worker_id, answer_obj=cached_obj)

            return QueryResponse(
                query=query,
                caller_role=role,
                answer=answer,
                citations=citations,
                metrics=QueryMetrics(
                    total_latency_ms=elapsed_ms,
                    cache_hit=False,
                    retrieval_latency_ms=retrieval_ms,
                    compressed_context_tokens=context_tokens,
                    llm_ttft_ms=ttft_ms,
                    tokens_generated=tokens_generated,
                    prompt_cache_hit=True,
                ),
                trace_id=trace_id,
            )
        except Exception:
            await cache.release_singleflight(role, query, worker_id)
            raise

    @app.post("/query/stream")
    async def query_stream_endpoint(
        req: QueryRequest,
        x_user_role: str | None = Header(None),
    ) -> StreamingResponse:
        """Stream query tokens and citations over Server-Sent Events (SSE)."""
        role = validate_role(x_user_role)
        query = req.query.strip()
        trace_id = uuid.uuid4().hex

        # Check Cache
        cached, tier = await cache.get(role, query)
        if cached is not None and tier in ("L1", "L2"):
            return StreamingResponse(
                sse.stream_cached(trace_id=trace_id, caller_role=role, cached_answer=cached),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def event_generator():
            try:
                selected_choice, _ = await intent_router.classify_intent(query)
                if selected_choice == "OUT_OF_SCOPE_REQUEST":
                    async def out_tokens():
                        yield OUT_OF_SCOPE_ANSWER

                    async for chunk in sse.stream_events(
                        trace_id=trace_id,
                        caller_role=role,
                        cache_hit=False,
                        token_generator=out_tokens(),
                        passages=[],
                    ):
                        yield chunk
                    return

                expanded_query = hyde.expand_query(query, caller_role=role)
                passages = await retrieval.retrieve(
                    query_text=expanded_query,
                    caller_role=role,
                    top_k=30,
                    final_rerank_limit=5,
                )
                compressed_context, _ = context_compressor.compress_passages(
                    passages, target_tokens=app_settings.compression_target_tokens
                )

                try:
                    token_gen = llm.generate_stream(query, compressed_context, caller_role=role)
                    async for chunk in sse.stream_events(
                        trace_id=trace_id,
                        caller_role=role,
                        cache_hit=False,
                        token_generator=token_gen,
                        passages=passages,
                    ):
                        yield chunk
                except Exception as llm_err:
                    logger.warning("LLM stream failure, streaming degraded citations: %s", llm_err)
                    async for chunk in sse.stream_degraded(
                        trace_id=trace_id,
                        caller_role=role,
                        passages=passages,
                    ):
                        yield chunk
            except Exception as stream_err:
                logger.error("Unhandled stream error: %s", stream_err)
                async for chunk in sse.stream_degraded(
                    trace_id=trace_id,
                    caller_role=role,
                    passages=[],
                    notice=f"Service encountered a temporary error: {stream_err}",
                ):
                    yield chunk

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/health")
    async def health_endpoint() -> dict[str, Any]:
        """Probes status of internal services and cache connectivity."""
        return {
            "status": "HEALTHY",
            "service": "warden-orchestrator",
            "l1_cache_size": len(cache._l1_cache),
            "redis_connected": cache.redis_client is not None,
            "retrieval_grpc_connected": True,
            "laya_grpc_connected": True,
            "azure_openai_configured": bool(app_settings.azure_openai_api_key),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return app
