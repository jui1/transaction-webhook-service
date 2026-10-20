"""Runtime configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Turn postgres:// and postgresql:// URLs into the psycopg v3 driver URL."""
    cleaned = url.strip()
    if cleaned.startswith("postgres://"):
        return "postgresql+psycopg://" + cleaned.removeprefix("postgres://")
    if cleaned.startswith("postgresql://"):
        return "postgresql+psycopg://" + cleaned.removeprefix("postgresql://")
    return cleaned


class Settings(BaseSettings):
    """Settings from the environment. The defaults are for local development."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://webhook:webhook@localhost:5432/webhook"
    redis_url: str = "redis://localhost:6379/0"
    celery_task_queue: str = "transactions"
    processing_delay_seconds: float = Field(default=30.0, ge=0)
    processing_lease_seconds: float = Field(default=120.0, gt=0)
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=50, gt=0)
    outbox_visibility_timeout_seconds: float = Field(default=180.0, gt=0)

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def processing_lease_window_seconds(self) -> float:
        """How long a worker may hold a transaction: the sleep plus extra time."""
        return self.processing_delay_seconds + self.processing_lease_seconds

    @property
    def outbox_redelivery_window_seconds(self) -> float:
        """How long to wait before publishing a dispatched job again."""
        return max(
            self.outbox_visibility_timeout_seconds,
            self.processing_lease_window_seconds + 30,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
