import base64
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FINANCE_AI_",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+psycopg://finance_ai:change-me-local@localhost:5432/finance_ai"
    cors_allowed_origins: list[str] = Field(default_factory=list)
    staff_local_cookie_exception_enabled: bool = False
    staff_cloud_run_host: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9.-]*\.run\.app$"
    )
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
    llm_provider: Literal["gemini", "deepseek"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model: str = "gemini-3.8-flash"
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    llm_timeout_seconds: float = Field(default=30.0, ge=1, le=120)
    llm_max_output_tokens: int = Field(default=2_048, ge=256, le=8_192)
    llm_max_image_bytes: int = Field(default=1_048_576, ge=1_024, le=5_242_880)
    # SEC-01 staff TOTP. The key is a base64url-encoded 32-byte AES-256-GCM key supplied by
    # deployment configuration; without it privileged MFA sign-in fails closed.
    staff_mfa_encryption_key: SecretStr | None = None
    staff_mfa_issuer: str = Field(default="FinanciAI", pattern=r"^[A-Za-z0-9 ._-]{1,64}$")
    staff_mfa_challenge_minutes: int = Field(default=5, ge=1, le=15)
    staff_mfa_max_attempts: int = Field(default=5, ge=1, le=10)
    # Administrator sessions are shorter than other staff sessions (SEC-01).
    staff_admin_access_minutes: int = Field(default=10, ge=1, le=30)
    staff_admin_idle_minutes: int = Field(default=15, ge=5, le=60)
    staff_admin_absolute_hours: int = Field(default=4, ge=1, le=12)
    # ADM-03: one-time staff activation links expire after this many hours.
    staff_invitation_hours: int = Field(default=48, ge=1, le=168)
    # AUD-01..AUD-04 query/export bounds. They are request limits, not retention policy.
    audit_max_query_days: int = Field(default=90, ge=1, le=366)
    audit_max_report_events: int = Field(default=2_000, ge=1, le=10_000)

    @field_validator("staff_mfa_encryption_key", mode="before")
    @classmethod
    def blank_mfa_key_means_unconfigured(cls, value: object) -> object:
        # Compose passes unset variables as empty strings; that must not stop the API.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def reject_disposable_defaults_outside_local(self) -> Settings:
        if "*" in self.cors_allowed_origins:
            raise ValueError("CORS origins must be explicitly listed")
        if self.environment == "production" and "change-me-local" in self.database_url:
            raise ValueError("Disposable local database credentials cannot be used in production")
        if self.environment == "production" and self.session_limiter_secret.startswith("change-me"):
            raise ValueError("A non-disposable session limiter secret is required in production")
        if self.renewable_idle_lifetime_days > self.session_absolute_lifetime_days:
            raise ValueError("Renewable idle lifetime cannot exceed absolute session lifetime")
        if self.staff_admin_access_minutes > self.staff_admin_idle_minutes:
            raise ValueError("Administrator access lifetime cannot exceed its idle lifetime")
        if self.staff_admin_idle_minutes > self.staff_admin_absolute_hours * 60:
            raise ValueError("Administrator idle lifetime cannot exceed its absolute lifetime")
        if self.staff_mfa_encryption_key is not None and not _is_aes256_key(
            self.staff_mfa_encryption_key.get_secret_value()
        ):
            raise ValueError("The staff MFA encryption key must be 32 base64url-encoded bytes")
        return self

    @property
    def resolved_unsafe_passwords_path(self) -> Path:
        if self.unsafe_passwords_path is not None:
            return self.unsafe_passwords_path
        return Path(__file__).resolve().parents[1] / "resources" / "common-passwords.txt"


def _is_aes256_key(encoded: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(encoded.strip() + "=" * (-len(encoded) % 4))) == 32
    except ValueError:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
