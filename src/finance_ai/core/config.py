from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FINANCE_AI_",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+psycopg://finance_ai:change-me-local@localhost:5432/finance_ai"
    log_level: str = "INFO"
    max_request_body_bytes: int = Field(default=16 * 1024, ge=1024, le=1024 * 1024)
    registration_per_minute: int = Field(default=10, ge=1, le=10_000)
    registration_burst_per_10_seconds: int = Field(default=3, ge=1, le=1_000)
    registration_global_per_minute: int = Field(default=100, ge=1, le=100_000)
    login_per_minute: int = Field(default=10, ge=1, le=10_000)
    login_burst_per_10_seconds: int = Field(default=5, ge=1, le=1_000)
    login_account_per_minute: int = Field(default=5, ge=1, le=10_000)
    login_global_per_minute: int = Field(default=300, ge=1, le=100_000)
    refresh_per_minute: int = Field(default=30, ge=1, le=10_000)
    refresh_global_per_minute: int = Field(default=1_000, ge=1, le=100_000)
    session_limiter_secret: str = Field(
        default="change-me-local-session-limiter", min_length=16, max_length=256
    )
    access_lifetime_minutes: int = Field(default=15, ge=1, le=60)
    renewable_idle_lifetime_days: int = Field(default=30, ge=1, le=90)
    session_absolute_lifetime_days: int = Field(default=90, ge=1, le=365)
    max_active_sessions_per_user: int = Field(default=10, ge=1, le=100)
    terminal_session_retention_days: int = Field(default=30, ge=1, le=365)
    unsafe_passwords_path: Path | None = None
    argon2_memory_kib: int = Field(default=19_456, ge=19_456)
    argon2_time_cost: int = Field(default=2, ge=2)
    argon2_parallelism: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def reject_disposable_defaults_outside_local(self) -> Settings:
        if self.environment == "production" and "change-me-local" in self.database_url:
            raise ValueError("Disposable local database credentials cannot be used in production")
        if self.environment == "production" and self.session_limiter_secret.startswith("change-me"):
            raise ValueError("A non-disposable session limiter secret is required in production")
        if self.renewable_idle_lifetime_days > self.session_absolute_lifetime_days:
            raise ValueError("Renewable idle lifetime cannot exceed absolute session lifetime")
        return self

    @property
    def resolved_unsafe_passwords_path(self) -> Path:
        if self.unsafe_passwords_path is not None:
            return self.unsafe_passwords_path
        return Path(__file__).resolve().parents[1] / "resources" / "common-passwords.txt"


@lru_cache
def get_settings() -> Settings:
    return Settings()
