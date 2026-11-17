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
    # EML-01 email notifications. The console sender only writes previews to disk and never
    # delivers; production uses "smtp". Nothing here is required to start the API: without a
    # real sender configured, notification features report that they are unavailable.
    email_provider: Literal["console", "smtp"] = "console"
    email_from_address: str | None = None
    email_from_name: str = Field(default="Finance AI", min_length=1, max_length=80)
    email_reply_to: str | None = None
    email_console_directory: Path = Path("var/email-preview")
    email_smtp_host: str | None = None
    email_smtp_port: int = Field(default=587, ge=1, le=65_535)
    email_smtp_username: str | None = None
    email_smtp_password: SecretStr | None = None
    email_smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    email_smtp_timeout_seconds: float = Field(default=10.0, ge=1, le=60)
    # Public origin of this API, used only to build unsubscribe links inside emails.
    public_base_url: str | None = None
    # Shared secret for the scheduled dispatch endpoint. Empty keeps that endpoint disabled.
    notification_dispatch_token: SecretStr | None = None
    email_daily_cap_per_user: int = Field(default=10, ge=1, le=100)
    email_max_attempts: int = Field(default=5, ge=1, le=10)
    email_verification_minutes: int = Field(default=15, ge=5, le=60)
    email_verification_max_attempts: int = Field(default=5, ge=1, le=10)

    @field_validator(
        "email_from_address",
        "email_reply_to",
        "email_smtp_host",
        "email_smtp_username",
        "email_smtp_password",
        "public_base_url",
        "notification_dispatch_token",
        mode="before",
    )
    @classmethod
    def blank_email_settings_mean_unconfigured(cls, value: object) -> object:
        # Compose and Cloud Run pass unset variables as empty strings.
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
        self._validate_email_settings()
        return self

    def _validate_email_settings(self) -> None:
        for name in ("email_from_address", "email_reply_to"):
            address = getattr(self, name)
            if address is not None and not _is_plain_email_address(address):
                raise ValueError(f"{name} must be a single plain email address")
        if self.email_provider == "smtp":
            if self.email_smtp_host is None or self.email_from_address is None:
                raise ValueError("The SMTP email provider needs a host and a sender address")
            if (self.email_smtp_username is None) != (self.email_smtp_password is None):
                raise ValueError("SMTP username and password must be set together")
            if self.environment == "production" and self.email_smtp_security == "none":
                raise ValueError("Unencrypted SMTP cannot be used in production")
        if self.public_base_url is not None:
            allowed = self.environment != "production" and self.public_base_url.startswith(
                "http://"
            )
            if not (self.public_base_url.startswith("https://") or allowed):
                raise ValueError("The public base URL must use https outside local development")
            if self.public_base_url.endswith("/"):
                raise ValueError("The public base URL must not end with a slash")
        token = self.notification_dispatch_token
        if token is not None and len(token.get_secret_value()) < 32:
            raise ValueError("The notification dispatch token must have at least 32 characters")

    @property
    def resolved_unsafe_passwords_path(self) -> Path:
        if self.unsafe_passwords_path is not None:
            return self.unsafe_passwords_path
        return Path(__file__).resolve().parents[1] / "resources" / "common-passwords.txt"


def _is_plain_email_address(value: str) -> bool:
    """A bare address: no display name, no whitespace, no header-injection characters."""
    if any(character in value for character in "\r\n\t <>,;\"'"):
        return False
    local, separator, domain = value.partition("@")
    return bool(local) and separator == "@" and "." in domain and not domain.startswith(".")


def _is_aes256_key(encoded: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(encoded.strip() + "=" * (-len(encoded) % 4))) == 32
    except ValueError:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
