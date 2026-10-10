from functools import lru_cache
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    embedding_provider: Literal["auto", "gemini", "cloudflare"] = Field("auto", alias="EMBEDDING_PROVIDER")
    answer_provider: Literal["auto", "gemini", "groq"] = Field("auto", alias="ANSWER_PROVIDER")
    cloudflare_account_id: str = Field("", alias="CLOUDFLARE_ACCOUNT_ID")
    cloudflare_api_token: str = Field("", alias="CLOUDFLARE_API_TOKEN")
    cloudflare_embedding_model: str = Field("@cf/qwen/qwen3-embedding-0.6b", alias="CLOUDFLARE_EMBEDDING_MODEL")
    cloudflare_timeout_seconds: int = Field(60, ge=5, le=180, alias="CLOUDFLARE_TIMEOUT_SECONDS")
    groq_api_key: str = Field("", alias="GROQ_API_KEY")
    groq_model: str = Field("openai/gpt-oss-20b", alias="GROQ_MODEL")
    groq_timeout_seconds: int = Field(120, ge=10, le=180, alias="GROQ_TIMEOUT_SECONDS")
    groq_answer_max_evidence_chars: int = Field(10000, ge=4000, le=24000, alias="GROQ_ANSWER_MAX_EVIDENCE_CHARS")
    groq_max_completion_tokens: int = Field(3072, ge=512, le=8192, alias="GROQ_MAX_COMPLETION_TOKENS")
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
    embedding_batch_size: int = Field(8, ge=1, le=32)
    embedding_max_inputs_per_lease: int = Field(240, ge=1, alias="EMBEDDING_MAX_INPUTS_PER_LEASE")
    gemini_count_embedding_tokens: bool = Field(True, alias="GEMINI_COUNT_EMBEDDING_TOKENS")
    # Admission is project-wide; these limits are deliberately below the
    # screenshot's free-tier 100 RPM / 30K TPM after quota_fraction is applied.
    gemini_quota_project: str = Field("default", alias="GEMINI_QUOTA_PROJECT")
    gemini_quota_fraction: float = Field(0.70, ge=0.1, le=0.9, alias="GEMINI_QUOTA_FRACTION")
    gemini_embedding_rpm: int = Field(100, ge=1, alias="GEMINI_EMBEDDING_RPM")
    gemini_embedding_tpm: int = Field(30000, ge=1000, alias="GEMINI_EMBEDDING_TPM")
    gemini_embedding_daily_budget: int = Field(700, ge=1, alias="GEMINI_EMBEDDING_DAILY_BUDGET")
    gemini_embedding_rpd: int | None = Field(None, ge=1, alias="GEMINI_EMBEDDING_RPD")
    gemini_query_rpm_reserve: int = Field(10, ge=0, alias="GEMINI_QUERY_RPM_RESERVE")
    gemini_query_tpm_reserve: int = Field(3000, ge=0, alias="GEMINI_QUERY_TPM_RESERVE")
    gemini_query_daily_reserve: int = Field(25, ge=0, alias="GEMINI_QUERY_DAILY_RESERVE")
    gemini_answer_rpm: int = Field(5, ge=1, alias="GEMINI_ANSWER_RPM")
    gemini_answer_tpm: int = Field(250000, ge=1000, alias="GEMINI_ANSWER_TPM")
    gemini_answer_daily_budget: int = Field(100, ge=1, alias="GEMINI_ANSWER_DAILY_BUDGET")
    gemini_answer_rpd: int | None = Field(None, ge=1, alias="GEMINI_ANSWER_RPD")
    gemini_answer_max_evidence_chars: int = Field(24000, ge=4000, le=48000, alias="GEMINI_ANSWER_MAX_EVIDENCE_CHARS")
    repository_submissions_per_ip_day: int = Field(2, ge=1, alias="REPOSITORY_SUBMISSIONS_PER_IP_DAY")
    # Retained only for local-mode compatibility; production uses the Redis gate.
    embedding_inputs_per_minute: int = Field(80, ge=0, le=100000, alias="GEMINI_EMBEDDING_INPUTS_PER_MINUTE")
    embedding_batch_retries: int = Field(5, ge=0, le=8, alias="GEMINI_EMBEDDING_BATCH_RETRIES")
    vector_index: str = "chunks_vector_v1"
    search_index: str = "chunks_search_v1"
    chunk_profile_version: int = Field(2, ge=1, le=2, exclude=True)

    @property
    def effective_embedding_provider(self) -> str:
        if self.embedding_provider != "auto":
            return self.embedding_provider
        return "cloudflare" if self.cloudflare_account_id and self.cloudflare_api_token else "gemini"

    @property
    def effective_answer_provider(self) -> str:
        if self.answer_provider != "auto":
            return self.answer_provider
        return "groq" if self.groq_api_key else "gemini"

    @property
    def effective_embedding_model(self) -> str:
        return self.cloudflare_embedding_model if self.effective_embedding_provider == "cloudflare" else self.embedding_model

    @property
    def effective_embedding_dimensions(self) -> int:
        # The Cloudflare-hosted Qwen3 0.6B endpoint returns 1024-dimensional vectors.
        return 1024 if self.effective_embedding_provider == "cloudflare" else self.embedding_dimensions

    @property
    def effective_vector_index(self) -> str:
        return "chunks_vector_v2" if self.effective_embedding_provider == "cloudflare" else self.vector_index

    @property
    def effective_embedding_field(self) -> str:
        return "cloudflareEmbedding" if self.effective_embedding_provider == "cloudflare" else "embedding"

    @model_validator(mode="after")
    def validate_quota_reserves(self):
        rpm = math.floor(self.gemini_embedding_rpm * self.gemini_quota_fraction)
        tpm = math.floor(self.gemini_embedding_tpm * self.gemini_quota_fraction)
        daily = min(self.gemini_embedding_daily_budget,
                    math.floor(self.gemini_embedding_rpd * self.gemini_quota_fraction) if self.gemini_embedding_rpd else self.gemini_embedding_daily_budget)
        if rpm <= self.gemini_query_rpm_reserve or tpm <= self.gemini_query_tpm_reserve or daily <= self.gemini_query_daily_reserve:
            raise ValueError("Embedding quota budgets must exceed the capacity reserved for questions")
        return self

    @field_validator("data_dir", "demo_dir", "analyzer", mode="after")
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        # API, worker, and setup commands may start from different directories.
        # Resolve relative configuration against the checkout, never process cwd.
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def embedding_config(self) -> str:
        return f"{self.effective_embedding_model}:{self.effective_embedding_dimensions}:code-retrieval-v1"

    @property
    def analysis_profile(self) -> str:
        return f"typescript-v1:chunk-v{self.chunk_profile_version}:{self.embedding_config}"

    def for_analysis_profile(self, profile: str) -> "Settings":
        """Use an existing analysis's embedding space when the default changes."""
        try:
            prefix, model, dimensions, retrieval = profile.rsplit(":", 3)
            if retrieval != "code-retrieval-v1" or prefix not in {"typescript-v1:chunk-v1", "typescript-v1:chunk-v2"}:
                raise ValueError
            dimension_count = int(dimensions)
        except (ValueError, AttributeError) as error:
            raise ValueError("Unsupported analysis profile") from error
        if model.startswith("@cf/"):
            if dimension_count != 1024:
                raise ValueError("Unsupported Cloudflare embedding dimensions")
            return self.model_copy(update={"embedding_provider": "cloudflare", "cloudflare_embedding_model": model,
                                           "chunk_profile_version": int(prefix[-1])})
        return self.model_copy(update={"embedding_provider": "gemini", "embedding_model": model,
                                       "embedding_dimensions": dimension_count, "chunk_profile_version": int(prefix[-1])})


@lru_cache
def get_settings() -> Settings:
    return Settings()
