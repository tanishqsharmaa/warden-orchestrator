from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from warden_orchestrator.models import Passage
from warden_orchestrator.retrieval_client import RetrievalClient


@pytest.mark.asyncio
async def test_retrieval_client_success():
    client = RetrievalClient(grpc_target="localhost:50051")

    mock_passage = MagicMock()
    mock_passage.doc_id = "DOC-HR-LEAVE-2026"
    mock_passage.chunk_index = 2
    mock_passage.content = "Bereavement Leave: Employees are eligible for up to 5 days."
    mock_passage.source_url = "file:///data/pto.md"
    mock_passage.role_tags = ["Employee", "Manager"]
    mock_passage.rrf_score = 0.0328
    mock_passage.calibrated_score = 0.9421

    mock_resp = MagicMock()
    mock_resp.passages = [mock_passage]

    with patch.object(client, "_get_stub") as mock_get_stub:
        mock_stub = AsyncMock()
        mock_stub.Retrieve = AsyncMock(return_value=mock_resp)
        mock_get_stub.return_value = mock_stub

        passages = await client.retrieve(
            query_text="How many days of bereavement leave?",
            caller_role="Employee",
            top_k=30,
            final_rerank_limit=5,
            enable_reranker=True,
            timeout=2.0
        )

        assert len(passages) == 1
        p = passages[0]
        assert isinstance(p, Passage)
        assert p.doc_id == "DOC-HR-LEAVE-2026"
        assert p.chunk_index == 2
        assert p.calibrated_score == 0.9421
        assert "Bereavement Leave" in p.content

@pytest.mark.asyncio
async def test_retrieval_client_grpc_error_handling():
    client = RetrievalClient(grpc_target="localhost:50051")

    with patch.object(client, "_get_stub") as mock_get_stub:
        mock_stub = AsyncMock()
        mock_rpc_error = grpc.RpcError("Unavailable")
        mock_rpc_error.code = MagicMock(return_value=grpc.StatusCode.UNAVAILABLE)
        mock_rpc_error.details = MagicMock(return_value="Qdrant service offline")
        mock_stub.Retrieve = AsyncMock(side_effect=mock_rpc_error)
        mock_get_stub.return_value = mock_stub

        with pytest.raises(Exception) as exc_info:
            await client.retrieve("vacation", "Employee")
        assert "Unavailable" in str(exc_info.value) or "Qdrant" in str(exc_info.value)

@pytest.mark.asyncio
async def test_retrieval_client_close():
    client = RetrievalClient(grpc_target="localhost:50051")
    mock_channel = AsyncMock()
    client._channel = mock_channel
    await client.close()
    mock_channel.close.assert_awaited_once()
