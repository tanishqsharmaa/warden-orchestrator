"""Configuration settings for Project Warden Orchestrator (Tier 5)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration matrix for warden-orchestrator."""

    azure_openai_endpoint: str = "https://warden-ai.openai.azure.com/"
    azure_openai_api_key: str = "default-api-key-placeholder"
    azure_openai_deployment: str = "gpt-4.1-mini"
    azure_openai_api_version: str = "2024-08-01-preview"

    redis_url: str = "redis://warden-cache-redis:6379"

    retrieval_grpc_url: str = "warden-retrieval:50051"
    laya_grpc_url: str = "warden-laya-service:50051"

    l1_cache_capacity: int = 1000
    l1_cache_ttl_sec: int = 60
    l2_cache_ttl_sec: int = 1800

    prompt_cache_enabled: bool = True
    compression_target_tokens: int = 800
    max_context_tokens: int = 1500

    port: int = 8000
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get singleton cached application settings."""
    return Settings()
