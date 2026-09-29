import os
import pytest
from warden_orchestrator.config import Settings, get_settings

def test_settings_defaults():
    settings = Settings(
        azure_openai_endpoint="https://test.openai.azure.com/",
        azure_openai_api_key="test-key",
    )
    assert settings.azure_openai_deployment == "gpt-4.1-mini"
    assert settings.azure_openai_api_version == "2024-08-01-preview"
    assert settings.redis_url == "redis://warden-cache-redis:6379"
    assert settings.l1_cache_capacity == 1000
    assert settings.l1_cache_ttl_sec == 60
    assert settings.l2_cache_ttl_sec == 1800
    assert settings.compression_target_tokens == 800
    assert settings.max_context_tokens == 1500
    assert settings.prompt_cache_enabled is True
    assert settings.retrieval_grpc_url == "warden-retrieval:50051"
    assert settings.laya_grpc_url == "warden-laya-service:50051"
    assert settings.port == 8000
    assert settings.otel_exporter_otlp_endpoint == "http://otel-collector:4317"

def test_settings_singleton_caching():
    os.environ["AZURE_OPENAI_ENDPOINT"] = "https://test.openai.azure.com/"
    os.environ["AZURE_OPENAI_API_KEY"] = "test-key"
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
