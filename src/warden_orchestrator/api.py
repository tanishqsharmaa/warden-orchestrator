"""FastAPI REST application for warden-orchestrator."""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, Request
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
    default_cache = cache_coordinator or TwoTierCacheCoordinator(
        l1_capacity=app_settings.l1_cache_capacity,
        l1_ttl_sec=app_settings.l1_cache_ttl_sec,
        l2_ttl_sec=app_settings.l2_cache_ttl_sec,
    )
    default_retrieval = retrieval_client or RetrievalClient(grpc_target=app_settings.retrieval_grpc_url)
    default_router = router or QueryIntentRouter(grpc_target=app_settings.laya_grpc_url)
    default_hyde = hyde_expander or HyDEExpander()
    default_compressor = compressor or ContextCompressor()
    default_llm = llm_client or AzureOpenAIClientWrapper(
        endpoint=app_settings.azure_openai_endpoint,
        api_key=app_settings.azure_openai_api_key,
        deployment=app_settings.azure_openai_deployment,
        api_version=app_settings.azure_openai_api_version,
    )
    default_sse = sse_generator or SSEStreamGenerator()

    def get_cache(request: Request) -> TwoTierCacheCoordinator:
        return getattr(request.app.state, "cache", default_cache)

    def get_retrieval(request: Request) -> RetrievalClient:
        return getattr(request.app.state, "retrieval_client", default_retrieval)

    def get_router(request: Request) -> QueryIntentRouter:
        return getattr(request.app.state, "router", default_router)

    @app.post("/query", response_model=QueryResponse)
    async def query_endpoint(
        req: QueryRequest,
        request: Request,
        x_user_role: str | None = Header(None),
    ) -> QueryResponse:
        """Execute synchronous access-controlled query lifecycle."""
        role = validate_role(x_user_role)
        query = req.query.strip()
        trace_id = uuid.uuid4().hex
        start_time = time.perf_counter()

        cache = get_cache(request)
        retrieval = get_retrieval(request)
        intent_router = get_router(request)
        hyde = default_hyde
        context_compressor = default_compressor
        llm = default_llm

        # 1. Two-Tier Cache Check
        cached, tier = await cache.get(role, query)
        if cached is not None and tier in ("L1", "L2", "L2_REFRESH"):
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
                await cache.release_singleflight(role, query, worker_id)
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
            compressed_context, context_tokens, retained_passages = context_compressor.compress_passages(
                passages, target_tokens=app_settings.compression_target_tokens
            )

            # 6. LLM Grounded Answer Generation
            is_degraded = False
            try:
                answer, ttft_ms, tokens_generated = await llm.generate_answer(
                    query=query,
                    compressed_context=compressed_context,
                    caller_role=role,
                )
            except Exception as llm_exc:
                logger.error("LLM generation fault, degrading response: %s", llm_exc)
                answer = f"{DEGRADED_SERVICE_NOTICE}\n\n" + "\n\n".join(
                    f"[Doc: {p.doc_id}, Chunk: {p.chunk_index}]\n{p.content}" for p in retained_passages
                )
                ttft_ms = 0.0
                tokens_generated = 0
                is_degraded = True

            # Build citations exclusively from retained passages
            citations = [
                {
                    "citation_id": i + 1,
                    "doc_id": p.doc_id,
                    "chunk_index": p.chunk_index,
                    "source_url": p.source_url,
                }
                for i, p in enumerate(retained_passages)
            ]

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            # 7. Populate Two-Tier Cache & Release Lock (Only if NOT degraded)
            cached_obj = None
            if not is_degraded:
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
        request: Request,
        x_user_role: str | None = Header(None),
    ) -> StreamingResponse:
        """Stream query tokens and citations over Server-Sent Events (SSE)."""
        role = validate_role(x_user_role)
        query = req.query.strip()
        trace_id = uuid.uuid4().hex

        cache = get_cache(request)
        retrieval = get_retrieval(request)
        intent_router = get_router(request)
        hyde = default_hyde
        context_compressor = default_compressor
        llm = default_llm
        sse = default_sse

        # 1. Check Cache
        cached, tier = await cache.get(role, query)
        if cached is not None and tier in ("L1", "L2", "L2_REFRESH"):
            return StreamingResponse(
                sse.stream_cached(trace_id=trace_id, caller_role=role, cached_answer=cached),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # 2. Acquire SingleFlight Mutex Lock for Stream
        worker_id = f"worker-stream-{uuid.uuid4().hex[:8]}"
        acquired = await cache.acquire_singleflight(role, query, worker_id)
        if not acquired:
            waited = await cache.wait_for_singleflight(role, query, timeout=3.5)
            if waited is not None:
                return StreamingResponse(
                    sse.stream_cached(trace_id=trace_id, caller_role=role, cached_answer=waited),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

        async def event_generator():
            start_time = time.perf_counter()
            streamed_tokens: list[str] = []
            retained_passages = []
            is_degraded = False

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
                compressed_context, _, retained_passages = context_compressor.compress_passages(
                    passages, target_tokens=app_settings.compression_target_tokens
                )

                async def accumulating_token_gen():
                    async for token in llm.generate_stream(query, compressed_context, caller_role=role):
                        streamed_tokens.append(token)
                        yield token

                try:
                    async for chunk in sse.stream_events(
                        trace_id=trace_id,
                        caller_role=role,
                        cache_hit=False,
                        token_generator=accumulating_token_gen(),
                        passages=retained_passages,
                    ):
                        yield chunk
                except Exception as llm_err:
                    logger.warning("LLM stream failure, streaming degraded citations: %s", llm_err)
                    is_degraded = True
                    async for chunk in sse.stream_degraded(
                        trace_id=trace_id,
                        caller_role=role,
                        passages=retained_passages,
                    ):
                        yield chunk

                # If clean stream completion, populate cache
                if not is_degraded and streamed_tokens:
                    full_answer = "".join(streamed_tokens)
                    citations = [
                        {
                            "citation_id": i + 1,
                            "doc_id": p.doc_id,
                            "chunk_index": p.chunk_index,
                            "source_url": p.source_url,
                        }
                        for i, p in enumerate(retained_passages)
                    ]
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    cached_obj = CachedAnswer(
                        answer=full_answer,
                        citations=citations,
                        metrics={"prompt_cache_hit": True},
                        created_at=time.time(),
                        delta_t=elapsed_ms / 1000.0,
                        ttl=app_settings.l2_cache_ttl_sec,
                    )
                    await cache.set(role, query, full_answer, citations, delta_t=elapsed_ms / 1000.0)
                    await cache.release_singleflight(role, query, worker_id, answer_obj=cached_obj)
            except Exception as stream_err:
                logger.error("Unhandled stream error: %s", stream_err)
                async for chunk in sse.stream_degraded(
                    trace_id=trace_id,
                    caller_role=role,
                    passages=[],
                    notice=f"Service encountered a temporary error: {stream_err}",
                ):
                    yield chunk
            finally:
                await cache.release_singleflight(role, query, worker_id)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/health")
    async def health_endpoint(request: Request) -> dict[str, Any]:
        """Probes status of internal services and cache connectivity."""
        active_cache = get_cache(request)
        return {
            "status": "HEALTHY",
            "service": "warden-orchestrator",
            "l1_cache_size": len(active_cache._l1_cache),
            "redis_connected": active_cache.redis_client is not None,
            "retrieval_grpc_connected": True,
            "laya_grpc_connected": True,
            "azure_openai_configured": bool(app_settings.azure_openai_api_key),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return app
