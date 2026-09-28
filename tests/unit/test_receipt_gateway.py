"""Unit tests for provider gateway error hygiene (no upstream payloads leave the service)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from pydantic import SecretStr

from finance_ai.core.config import Settings
from finance_ai.receipts.gateway import GeminiVisionAdapter, VisionProviderError

UPSTREAM_BODY = (
    '{"error":{"message":"API key not valid. Please pass a valid API key.",'
    '"status":"INVALID_ARGUMENT","internal_ref":"provider-internal-ref-88231"}}'
)
MODEL_OUTPUT_MARKER = "SUPERMERCADO-EXITO-TOTAL-154900-INTERNAL"


class _FakeResponse:
    def __init__(self, *, status_code: int, text: str, payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("Response body is not JSON")
        return self._payload


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in that always returns the same response."""

    def __init__(self, response: _FakeResponse, **_kwargs: Any) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return self._response


def _gemini_adapter(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> GeminiVisionAdapter:
    monkeypatch.setattr(
        "finance_ai.receipts.gateway.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response, **kwargs),
    )
    settings = Settings(
        _env_file=None,
        gemini_api_key=SecretStr("test-gemini-key"),
        deepseek_api_key=None,
    )
    return GeminiVisionAdapter(settings)


def test_provider_error_response_body_never_leaks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _gemini_adapter(monkeypatch, _FakeResponse(status_code=400, text=UPSTREAM_BODY))

    with caplog.at_level(logging.WARNING), pytest.raises(VisionProviderError) as exc_info:
        asyncio.run(adapter.extract_receipt_dto(b"jpeg-bytes"))

    # The raised error carries the status code only.
    assert exc_info.value.status_code == 400
    assert "400" in str(exc_info.value)
    assert "internal_ref" not in str(exc_info.value)
    assert "API key not valid" not in str(exc_info.value)

    # Nothing from the upstream body reaches the logs either.
    assert UPSTREAM_BODY not in caplog.text
    assert "provider-internal-ref-88231" not in caplog.text
    assert "API key not valid" not in caplog.text
    assert any(
        getattr(record, "event", None) == "receipt.extraction.provider_rejected"
        for record in caplog.records
    )


def test_unparseable_completion_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {"choices": [{"message": {"content": f"{MODEL_OUTPUT_MARKER} is not JSON"}}]}
    adapter = _gemini_adapter(
        monkeypatch, _FakeResponse(status_code=200, text="{}", payload=payload)
    )

    with caplog.at_level(logging.WARNING), pytest.raises(ValueError) as exc_info:
        asyncio.run(adapter.extract_receipt_dto(b"jpeg-bytes"))

    assert "not valid JSON" in str(exc_info.value)
    assert MODEL_OUTPUT_MARKER not in caplog.text
    assert MODEL_OUTPUT_MARKER not in str(exc_info.value)
    assert any(
        getattr(record, "event", None) == "receipt.extraction.invalid_json"
        for record in caplog.records
    )
