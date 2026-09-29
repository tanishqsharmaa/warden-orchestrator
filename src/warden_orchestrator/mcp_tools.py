"""Model Context Protocol (MCP) tool adapter for warden-orchestrator."""

from typing import Any
import logging

logger = logging.getLogger("warden.orchestrator.mcp")

TOOL_DEFINITIONS = [
    {
        "name": "search_policies",
        "description": "Executes access-controlled hybrid retrieval with calibrated ModernBERT reranking over company HR policy documents.",
        "inputSchema": {
            "type": "object",
            "required": ["query_text", "caller_role"],
            "properties": {
                "query_text": {
                    "type": "string",
                    "description": "The search query",
                },
                "caller_role": {
                    "type": "string",
                    "enum": ["Employee", "Manager", "HR-Admin"],
                    "description": "Caller security role tier",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "Number of top ranked passages to return",
                },
            },
        },
    },
    {
        "name": "route_query",
        "description": "Classifies incoming query intent using Laya ModernBERT non-generative choice primitives.",
        "inputSchema": {
            "type": "object",
            "required": ["query", "choices"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Incoming user inquiry text",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of candidate intent labels",
                },
            },
        },
    },
]


class MCPToolAdapter:
    """Adapts internal services to the Model Context Protocol (MCP) JSON-RPC standard."""

    def __init__(self, retrieval_client: Any, router: Any) -> None:
        self.retrieval_client = retrieval_client
        self.router = router

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return MCP tool schemas."""
        return TOOL_DEFINITIONS

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool invocation by name."""
        if name == "search_policies":
            query_text = arguments["query_text"]
            caller_role = arguments["caller_role"]
            top_k = arguments.get("top_k", 5)

            passages = await self.retrieval_client.retrieve(
                query_text=query_text,
                caller_role=caller_role,
                final_rerank_limit=top_k,
            )
            return {
                "passages": [
                    {
                        "doc_id": p.doc_id,
                        "chunk_index": p.chunk_index,
                        "content": p.content,
                        "source_url": p.source_url,
                        "rrf_score": p.rrf_score,
                        "calibrated_score": p.calibrated_score,
                    }
                    for p in passages
                ]
            }
        elif name == "route_query":
            query = arguments["query"]
            choices = arguments.get("choices")
            if choices:
                selected, dist = await self.router.classify_intent(query=query, choices=choices)
            else:
                selected, dist = await self.router.classify_intent(query=query)
            return {
                "selected_choice": selected,
                "confidence_distribution": dist,
            }
        else:
            raise ValueError(f"Unknown tool: {name}")
