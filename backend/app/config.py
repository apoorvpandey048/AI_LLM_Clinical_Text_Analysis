"""
Configuration management for SNAP-AI.

All configuration is loaded from environment variables.
See .env.example for available options.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Environment
    environment: Literal["development", "production"] = "development"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 4

    # Database
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "snapai"
    postgres_user: str = "snapai"
    postgres_password: str = "snapai_dev"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""

    # LLM Backend
    llm_backend: Literal["ollama", "vllm"] = "ollama"

    # Ollama (Development)
    ollama_host: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:7b-instruct-q4_K_M"

    # vLLM (Production)
    vllm_host: str = "http://host.docker.internal:8000"
    vllm_model: str = "openai/gpt-oss-120b"

    # LLM Inference Settings
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout: int = 900  # 15 minutes for large model
    llm_reasoning_level: str = "medium"  # low/medium/high for GPT-OSS-120B

    # Upload Settings
    max_upload_size_mb: int = 50

    # Authentication
    jwt_secret: str = "CHANGE-ME-set-JWT_SECRET-in-env"
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24
    admin_email: str = "admin@snapai.local"
    admin_password: str = "CHANGE-ME-set-ADMIN_PASSWORD-in-env"

    # CORS
    cors_origins: str = "*"  # Comma-separated origins, or "*" for dev

    # Logging
    log_level: str = "INFO"

    # Versioning
    prompt_version: str = "1.10"

    @property
    def database_url(self) -> str:
        """Construct database URL from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Construct Redis URL from components."""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def llm_host(self) -> str:
        """Get the current LLM host based on backend."""
        if self.llm_backend == "ollama":
            return self.ollama_host
        return self.vllm_host

    @property
    def llm_model(self) -> str:
        """Get the current LLM model based on backend."""
        if self.llm_backend == "ollama":
            return self.ollama_model
        return self.vllm_model

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
