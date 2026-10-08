from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )
    mode: Literal["local", "production"] = Field("production", alias="NAVIGATOR_MODE")
    data_dir: Path = Field(PROJECT_ROOT / ".data", alias="NAVIGATOR_DATA_DIR")
    demo_dir: Path = Field(PROJECT_ROOT / "tests/fixtures/shop", alias="NAVIGATOR_DEMO_DIR")
    analyzer: Path = Field(PROJECT_ROOT / "analyzers/typescript/src/cli.mjs", alias="NAVIGATOR_ANALYZER")
    node_binary: str = Field("node", alias="NODE_BINARY")
    mongodb_uri: str = Field("", alias="MONGODB_URI")
    mongodb_database: str = Field("codebase_navigator", alias="MONGODB_DATABASE")
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-3.5-flash", alias="GEMINI_MODEL")
    gemini_timeout_seconds: int = Field(120, ge=10, le=180, alias="GEMINI_TIMEOUT_SECONDS")
    gemini_thinking_level: Literal["low", "medium", "high"] = Field("low", alias="GEMINI_THINKING_LEVEL")
    embedding_model: str = Field("gemini-embedding-2", alias="GEMINI_EMBEDDING_MODEL")
    github_token: str = Field("", alias="GITHUB_TOKEN")
    allowed_origins: str = Field("http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://localhost:8080,http://127.0.0.1:8080", alias="NAVIGATOR_ALLOWED_ORIGINS")
    allowed_hosts: str = Field("localhost,127.0.0.1,testserver,api", alias="NAVIGATOR_ALLOWED_HOSTS")
    max_files: int = 2000
    max_text_bytes: int = 25 * 1024 * 1024
    max_file_bytes: int = 512 * 1024
    max_archive_bytes: int = 100 * 1024 * 1024
    max_expanded_bytes: int = 500 * 1024 * 1024
    max_chunks: int = 15000
    analyzer_timeout: int = 300
    analyzer_memory_mb: int = Field(1024, ge=128, le=4096)
    embedding_dimensions: int = 768
    embedding_batch_size: int = Field(16, ge=1, le=32)
    embedding_inputs_per_minute: int = Field(80, ge=0, le=100000, alias="GEMINI_EMBEDDING_INPUTS_PER_MINUTE")
    embedding_batch_retries: int = Field(5, ge=0, le=8, alias="GEMINI_EMBEDDING_BATCH_RETRIES")
    vector_index: str = "chunks_vector_v1"
    search_index: str = "chunks_search_v1"

    @field_validator("data_dir", "demo_dir", "analyzer", mode="after")
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        # API, worker, and setup commands may start from different directories.
        # Resolve relative configuration against the checkout, never process cwd.
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def embedding_config(self) -> str:
        return f"{self.embedding_model}:{self.embedding_dimensions}:code-retrieval-v1"

    @property
    def analysis_profile(self) -> str:
        return f"typescript-v1:chunk-v1:{self.embedding_config}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
