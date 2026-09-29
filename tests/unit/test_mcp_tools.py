import pytest
from unittest.mock import AsyncMock
from warden_orchestrator.models import Passage
from warden_orchestrator.mcp_tools import MCPToolAdapter

def test_mcp_tool_definitions():
    adapter = MCPToolAdapter(retrieval_client=AsyncMock(), router=AsyncMock())
    defs = adapter.get_tool_definitions()
    assert len(defs) == 2
    tool_names = [d["name"] for d in defs]
    assert "search_policies" in tool_names
    assert "route_query" in tool_names

    search_tool = next(d for d in defs if d["name"] == "search_policies")
    assert "query_text" in search_tool["inputSchema"]["required"]
    assert "caller_role" in search_tool["inputSchema"]["required"]

    route_tool = next(d for d in defs if d["name"] == "route_query")
    assert "query" in route_tool["inputSchema"]["required"]
    assert "choices" in route_tool["inputSchema"]["required"]

@pytest.mark.asyncio
async def test_mcp_execute_search_policies():
    mock_retrieval = AsyncMock()
    mock_retrieval.retrieve.return_value = [
        Passage(doc_id="DOC-1", chunk_index=0, content="PTO Policy text", source_url="file:///pto.md", rrf_score=0.04, calibrated_score=0.95)
    ]
    adapter = MCPToolAdapter(retrieval_client=mock_retrieval, router=AsyncMock())
    result = await adapter.execute_tool("search_policies", {"query_text": "PTO", "caller_role": "Employee", "top_k": 5})

    assert "passages" in result
    assert len(result["passages"]) == 1
    assert result["passages"][0]["doc_id"] == "DOC-1"
    mock_retrieval.retrieve.assert_awaited_once_with(
        query_text="PTO",
        caller_role="Employee",
        final_rerank_limit=5
    )

@pytest.mark.asyncio
async def test_mcp_execute_route_query():
    mock_router = AsyncMock()
    mock_router.classify_intent.return_value = (
        "HR_POLICY_QUESTION",
        {"HR_POLICY_QUESTION": 0.95, "OUT_OF_SCOPE_REQUEST": 0.05}
    )
    adapter = MCPToolAdapter(retrieval_client=AsyncMock(), router=mock_router)
    result = await adapter.execute_tool("route_query", {"query": "How many days off?", "choices": ["HR_POLICY_QUESTION", "OUT_OF_SCOPE_REQUEST"]})

    assert result["selected_choice"] == "HR_POLICY_QUESTION"
    assert "confidence_distribution" in result

@pytest.mark.asyncio
async def test_mcp_execute_unknown_tool():
    adapter = MCPToolAdapter(retrieval_client=AsyncMock(), router=AsyncMock())
    with pytest.raises(ValueError, match="Unknown tool"):
        await adapter.execute_tool("unknown_tool", {})
