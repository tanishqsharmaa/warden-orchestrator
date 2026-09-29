from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from warden_orchestrator.router import QueryIntentRouter


@pytest.mark.asyncio
async def test_classify_intent_success():
    router = QueryIntentRouter(grpc_target="localhost:50051")

    mock_resp = MagicMock()
    mock_resp.selected_choice = "HR_POLICY_QUESTION"
    mock_resp.confidence_distribution = {
        "HR_POLICY_QUESTION": 0.92,
        "COMPENSATION_INQUIRY": 0.04,
        "OUT_OF_SCOPE_REQUEST": 0.02,
        "DISCIPLINARY_ACTION": 0.02,
    }

    with patch.object(router, "_get_stub") as mock_get_stub:
        mock_stub = AsyncMock()
        mock_stub.Route = AsyncMock(return_value=mock_resp)
        mock_get_stub.return_value = mock_stub

        choice, dist = await router.classify_intent("How many PTO days do I get?")
        assert choice == "HR_POLICY_QUESTION"
        assert dist["HR_POLICY_QUESTION"] == 0.92

@pytest.mark.asyncio
async def test_classify_intent_out_of_scope():
    router = QueryIntentRouter(grpc_target="localhost:50051")

    mock_resp = MagicMock()
    mock_resp.selected_choice = "OUT_OF_SCOPE_REQUEST"
    mock_resp.confidence_distribution = {
        "HR_POLICY_QUESTION": 0.01,
        "OUT_OF_SCOPE_REQUEST": 0.98,
    }

    with patch.object(router, "_get_stub") as mock_get_stub:
        mock_stub = AsyncMock()
        mock_stub.Route = AsyncMock(return_value=mock_resp)
        mock_get_stub.return_value = mock_stub

        choice, dist = await router.classify_intent("Can you write python code for my side project?")
        assert choice == "OUT_OF_SCOPE_REQUEST"
        assert dist["OUT_OF_SCOPE_REQUEST"] == 0.98

@pytest.mark.asyncio
async def test_classify_intent_grpc_error_fallback():
    router = QueryIntentRouter(grpc_target="localhost:50051")

    with patch.object(router, "_get_stub") as mock_get_stub:
        mock_stub = AsyncMock()
        mock_rpc_error = grpc.RpcError("Deadline Exceeded")
        mock_rpc_error.code = MagicMock(return_value=grpc.StatusCode.DEADLINE_EXCEEDED)
        mock_stub.Route = AsyncMock(side_effect=mock_rpc_error)
        mock_get_stub.return_value = mock_stub

        # Falls open to default HR_POLICY_QUESTION so search continues
        choice, dist = await router.classify_intent("Any question during Laya outage")
        assert choice == "HR_POLICY_QUESTION"
        assert dist["HR_POLICY_QUESTION"] == 1.0

@pytest.mark.asyncio
async def test_router_close():
    router = QueryIntentRouter(grpc_target="localhost:50051")
    mock_channel = AsyncMock()
    router._channel = mock_channel
    await router.close()
    mock_channel.close.assert_awaited_once()
