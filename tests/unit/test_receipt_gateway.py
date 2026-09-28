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


def _capture_gateway_logs() -> tuple[logging.Handler, list[logging.LogRecord]]:
    """Captures records from the gateway logger directly.

    pytest's caplog handler lives on the root logger, which the application clears in
    configure_logging(), so a local handler keeps these assertions order independent.
    """
    records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CaptureHandler(level=logging.DEBUG)
    logging.getLogger("finance_ai.receipts.gateway").addHandler(handler)
    return handler, records


def test_provider_error_response_body_never_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _gemini_adapter(monkeypatch, _FakeResponse(status_code=400, text=UPSTREAM_BODY))
    handler, records = _capture_gateway_logs()
    try:
        with pytest.raises(VisionProviderError) as exc_info:
            asyncio.run(adapter.extract_receipt_dto(b"jpeg-bytes"))
    finally:
        logging.getLogger("finance_ai.receipts.gateway").removeHandler(handler)

    # The raised error carries the status code only.
    assert exc_info.value.status_code == 400
    assert "400" in str(exc_info.value)
    assert "internal_ref" not in str(exc_info.value)
    assert "API key not valid" not in str(exc_info.value)

    # Nothing from the upstream body reaches the logs either.
    rendered = "\n".join(record.getMessage() for record in records)
    assert UPSTREAM_BODY not in rendered
    assert "provider-internal-ref-88231" not in rendered
    assert "API key not valid" not in rendered
    assert any(
        getattr(record, "event", None) == "receipt.extraction.provider_rejected"
        for record in records
    )


def test_unparseable_completion_is_not_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"choices": [{"message": {"content": f"{MODEL_OUTPUT_MARKER} is not JSON"}}]}
    adapter = _gemini_adapter(
        monkeypatch, _FakeResponse(status_code=200, text="{}", payload=payload)
    )
    handler, records = _capture_gateway_logs()
    try:
        with pytest.raises(ValueError) as exc_info:
            asyncio.run(adapter.extract_receipt_dto(b"jpeg-bytes"))
    finally:
        logging.getLogger("finance_ai.receipts.gateway").removeHandler(handler)

    assert "not valid JSON" in str(exc_info.value)
    rendered = "\n".join(record.getMessage() for record in records)
    assert MODEL_OUTPUT_MARKER not in rendered
    assert MODEL_OUTPUT_MARKER not in str(exc_info.value)
    assert any(
        getattr(record, "event", None) == "receipt.extraction.invalid_json" for record in records
    )
