from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import RateLimitError

from warden_orchestrator.llm_client import AzureOpenAIClientWrapper


def test_system_prompt_prefix_exceeds_1024_tokens():
    client = AzureOpenAIClientWrapper(
        endpoint="https://mock.openai.azure.com/",
        api_key="mock-key",
        deployment="gpt-4.1-mini",
    )
    prefix = client.get_prompt_prefix()
    token_count = client.calculate_prefix_tokens()

    assert token_count >= 1024
    assert "Enterprise Human Resources Grounded Knowledge Assistant" in prefix
    assert "GROUNDING & FACTUAL INTEGRITY MANDATES" in prefix
    assert "[Doc: <doc_id>, Chunk: <chunk_index>]" in prefix

@pytest.mark.asyncio
async def test_generate_stream_success():
    client = AzureOpenAIClientWrapper(
        endpoint="https://mock.openai.azure.com/",
        api_key="mock-key",
        deployment="gpt-4.1-mini",
    )

    # Mock chunk stream from AsyncOpenAI
    async def mock_chunk_stream():
        chunks = ["Full-time", " employees", " receive", " 12 weeks."]
        for c in chunks:
            chunk_mock = MagicMock()
            choice_mock = MagicMock()
            choice_mock.delta.content = c
            chunk_mock.choices = [choice_mock]
            yield chunk_mock

    with patch.object(client, "_get_client") as mock_get_client:
        mock_ai = MagicMock()
        mock_ai.chat.completions.create = AsyncMock(return_value=mock_chunk_stream())
        mock_get_client.return_value = mock_ai

        tokens = []
        async for token in client.generate_stream(
            query="Parental leave duration?",
            compressed_context="[Doc: DOC-1, Chunk: 0]\nEmployees receive 12 weeks of parental leave.",
            caller_role="Employee"
        ):
            tokens.append(token)

        assert "".join(tokens) == "Full-time employees receive 12 weeks."

@pytest.mark.asyncio
async def test_generate_answer_measures_ttft_and_tokens():
    client = AzureOpenAIClientWrapper(
        endpoint="https://mock.openai.azure.com/",
        api_key="mock-key",
        deployment="gpt-4.1-mini",
    )

    async def mock_chunk_stream():
        chunks = ["Hello", " world"]
        for c in chunks:
            chunk_mock = MagicMock()
            choice_mock = MagicMock()
            choice_mock.delta.content = c
            chunk_mock.choices = [choice_mock]
            yield chunk_mock

    with patch.object(client, "_get_client") as mock_get_client:
        mock_ai = MagicMock()
        mock_ai.chat.completions.create = AsyncMock(return_value=mock_chunk_stream())
        mock_get_client.return_value = mock_ai

        answer, ttft_ms, token_count = await client.generate_answer(
            query="test",
            compressed_context="context",
            caller_role="Employee"
        )
        assert answer == "Hello world"
        assert ttft_ms >= 0.0
        assert token_count == 2

@pytest.mark.asyncio
async def test_retry_on_429_then_succeed():
    client = AzureOpenAIClientWrapper(
        endpoint="https://mock.openai.azure.com/",
        api_key="mock-key",
        deployment="gpt-4.1-mini",
    )

    async def mock_chunk_stream():
        chunk_mock = MagicMock()
        choice_mock = MagicMock()
        choice_mock.delta.content = "Success after retry"
        chunk_mock.choices = [choice_mock]
        yield chunk_mock

    err_response = MagicMock()
    err_response.status_code = 429
    err_response.headers = {}
    rate_limit_err = RateLimitError("Rate limit exceeded", response=err_response, body=None)

    with patch.object(client, "_get_client") as mock_get_client:
        mock_ai = MagicMock()
        mock_ai.chat.completions.create = AsyncMock(
            side_effect=[rate_limit_err, mock_chunk_stream()]
        )
        mock_get_client.return_value = mock_ai

        tokens = []
        async for token in client.generate_stream("query", "context", "Employee"):
            tokens.append(token)

        assert "".join(tokens) == "Success after retry"
        assert mock_ai.chat.completions.create.call_count == 2

@pytest.mark.asyncio
async def test_raises_rate_limit_error_after_max_retries():
    client = AzureOpenAIClientWrapper(
        endpoint="https://mock.openai.azure.com/",
        api_key="mock-key",
        deployment="gpt-4.1-mini",
    )

    err_response = MagicMock()
    err_response.status_code = 429
    err_response.headers = {}
    rate_limit_err = RateLimitError("Rate limit exceeded", response=err_response, body=None)

    with patch.object(client, "_get_client") as mock_get_client:
        mock_ai = MagicMock()
        mock_ai.chat.completions.create = AsyncMock(side_effect=rate_limit_err)
        mock_get_client.return_value = mock_ai

        with pytest.raises(RateLimitError):
            async for _ in client.generate_stream("query", "context", "Employee"):
                pass
        # 1 initial + 2 retries = 3 calls
        assert mock_ai.chat.completions.create.call_count == 3
