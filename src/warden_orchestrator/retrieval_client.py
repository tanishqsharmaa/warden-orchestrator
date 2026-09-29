"""gRPC client for warden-retrieval service."""

import logging
from typing import Any
import grpc

from warden_shared.proto.v1 import retrieval_pb2 as pb2
from warden_shared.proto.v1 import retrieval_pb2_grpc as pb2_grpc
from warden_orchestrator.models import Passage

logger = logging.getLogger("warden.orchestrator.retrieval_client")


class RetrievalClient:
    """High-throughput gRPC client consuming RetrievalService.Retrieve."""

    def __init__(self, grpc_target: str = "warden-retrieval:50051") -> None:
        self.grpc_target = grpc_target
        self._channel: grpc.aio.Channel | None = None
        self._stub: pb2_grpc.RetrievalServiceStub | None = None

    def _get_stub(self) -> pb2_grpc.RetrievalServiceStub:
        if self._channel is None or self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                self.grpc_target,
                options=[
                    ("grpc.keepalive_time_ms", 30000),
                    ("grpc.keepalive_timeout_ms", 5000),
                    ("grpc.http2.max_pings_without_data", 0),
                    ("grpc.max_receive_message_length", 32 * 1024 * 1024),
                ],
            )
            self._stub = pb2_grpc.RetrievalServiceStub(self._channel)
        return self._stub

    async def retrieve(
        self,
        query_text: str,
        caller_role: str,
        top_k: int = 30,
        final_rerank_limit: int = 5,
        enable_reranker: bool = True,
        timeout: float = 5.0,
    ) -> list[Passage]:
        """Dispatch access-controlled hybrid retrieval over gRPC."""
        stub = self._get_stub()
        req = pb2.RetrieveRequest(
            query_text=query_text,
            caller_role=caller_role,
            top_k_candidates=top_k,
            final_rerank_limit=final_rerank_limit,
            enable_reranker=enable_reranker,
        )

        try:
            resp: pb2.RetrieveResponse = await stub.Retrieve(req, timeout=timeout)
            passages: list[Passage] = []
            for p in resp.passages:
                passages.append(
                    Passage(
                        doc_id=p.doc_id,
                        chunk_index=p.chunk_index,
                        content=p.content,
                        source_url=p.source_url,
                        role_tags=list(p.role_tags),
                        rrf_score=p.rrf_score,
                        calibrated_score=p.calibrated_score,
                    )
                )
            return passages
        except grpc.RpcError as exc:
            logger.error("Retrieval gRPC call failed: code=%s, details=%s", exc.code(), exc.details())
            raise

    async def close(self) -> None:
        """Close the underlying gRPC channel."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
