import pytest
from pydantic import SecretStr, ValidationError

from finance_ai.core.config import Settings


def test_llm_defaults_select_gemini_and_verified_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCE_AI_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("FINANCE_AI_GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("FINANCE_AI_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("FINANCE_AI_DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("FINANCE_AI_DEEPSEEK_MODEL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "gemini"
    assert settings.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-flash"
    assert settings.gemini_api_key is None
    assert settings.deepseek_api_key is None


def test_llm_secrets_are_masked_and_provider_is_switchable() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key=SecretStr("sk-test-not-a-real-key"),
    )

    assert settings.llm_provider == "deepseek"
    assert settings.deepseek_api_key is not None
    assert settings.deepseek_api_key.get_secret_value() == "sk-test-not-a-real-key"
    assert "sk-test-not-a-real-key" not in repr(settings)


def test_llm_provider_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="openai")


def test_llm_tuning_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_max_output_tokens=100)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_max_image_bytes=10_000_000)
