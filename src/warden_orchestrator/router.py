"""Query intent router consuming Laya non-generative choice primitives."""

import logging
from typing import Any
import grpc

from warden_shared.proto.v1 import laya_pb2 as pb2
from warden_shared.proto.v1 import laya_pb2_grpc as pb2_grpc

logger = logging.getLogger("warden.orchestrator.router")

DEFAULT_INTENT_CHOICES = [
    "HR_POLICY_QUESTION",
    "COMPENSATION_INQUIRY",
    "OUT_OF_SCOPE_REQUEST",
    "DISCIPLINARY_ACTION",
]


class QueryIntentRouter:
    """Classifies user inquiries using Laya ModernBERT choice primitives over gRPC."""

    def __init__(self, grpc_target: str = "warden-laya-service:50051") -> None:
        self.grpc_target = grpc_target
        self._channel: grpc.aio.Channel | None = None
        self._stub: pb2_grpc.LayaInferenceServiceStub | None = None

    def _get_stub(self) -> pb2_grpc.LayaInferenceServiceStub:
        if self._channel is None or self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                self.grpc_target,
                options=[
                    ("grpc.keepalive_time_ms", 30000),
                    ("grpc.keepalive_timeout_ms", 5000),
                    ("grpc.http2.max_pings_without_data", 0),
                ],
            )
            self._stub = pb2_grpc.LayaInferenceServiceStub(self._channel)
        return self._stub

    async def classify_intent(
        self,
        query: str,
        choices: list[str] | None = None,
        timeout: float = 0.150,
    ) -> tuple[str, dict[str, float]]:
        """Classify incoming query intent. Returns (selected_choice, confidence_distribution)."""
        candidate_choices = choices or DEFAULT_INTENT_CHOICES
        stub = self._get_stub()
        req = pb2.RouteRequest(query=query, choices=candidate_choices)

        try:
            resp: pb2.RouteResponse = await stub.Route(req, timeout=timeout)
            return resp.selected_choice, dict(resp.confidence_distribution)
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else "UNKNOWN"
            details = exc.details() if hasattr(exc, "details") else str(exc)
            logger.warning(
                "Laya intent routing call failed: code=%s, details=%s (failing open to HR_POLICY_QUESTION)",
                code,
                details,
            )
            return "HR_POLICY_QUESTION", {"HR_POLICY_QUESTION": 1.0}
        except Exception as exc:
            logger.warning("Unexpected routing error: %s (failing open)", exc)
            return "HR_POLICY_QUESTION", {"HR_POLICY_QUESTION": 1.0}

    async def close(self) -> None:
        """Close gRPC channel."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
