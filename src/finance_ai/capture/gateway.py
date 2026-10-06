"""Text-only AI gateway for the HU-013 capture (DeepSeek first, Gemini as optional backup).

Both providers speak the OpenAI-compatible chat completions protocol. Prompts are built here,
secrets stay on the server, and failures are reduced to a status code or a failure kind: provider
bodies can echo the user's message and must never reach the client or the logs (AGENTS.md).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from finance_ai.capture.context import CaptureContext
from finance_ai.capture.interpretation import RawInterpretation
from finance_ai.capture.prompt import build_system_prompt, build_user_payload
from finance_ai.core.config import Settings, get_settings
from finance_ai.core.logging import get_logger
from finance_ai.receipts.gateway import check_json_depth, try_repair_json

logger = get_logger(__name__)

MAX_WIRE_BYTES = 256 * 1024
MAX_COMPLETION_CHARS = 32 * 1024
# The chat waits on this call, so the whole attempt (all providers) stays within one budget.
TOTAL_BUDGET_SECONDS = 25.0
MIN_ATTEMPT_SECONDS = 2.0

_provider_calls = asyncio.Semaphore(4)


class TextProviderError(RuntimeError):
    """A provider attempt failed. Carries only a failure kind and, when known, the HTTP status."""

    def __init__(self, kind: str, *, status_code: int | None = None) -> None:
        super().__init__(f"Text provider failure: {kind}")
        self.kind = kind
        self.status_code = status_code


class TextCaptureGateway(ABC):
    @abstractmethod
    async def interpret(
        self,
        *,
        context: CaptureContext,
        today: dt.date,
        original_text: str,
        follow_ups: list[str],
        previous: dict[str, Any] | None,
    ) -> RawInterpretation:
        """Reads the user's text and returns the model's interpretation of it."""


class OpenAICompatibleTextAdapter:
    """One OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}

    async def interpret(
        self,
        *,
        context: CaptureContext,
        today: dt.date,
        original_text: str,
        follow_ups: list[str],
        previous: dict[str, Any] | None,
        timeout: float,
    ) -> RawInterpretation:
        if not self.api_key:
            raise TextProviderError("not_configured")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_system_prompt(context, today)},
                {
                    "role": "user",
                    "content": build_user_payload(original_text, follow_ups, previous),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            **self.extra_body,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "FinanceAI-TextCapture/1.0",
        }

        started = time.perf_counter()
        try:
            async with _provider_calls, httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise TextProviderError("timeout") from exc
        except httpx.RequestError as exc:
            raise TextProviderError("network") from exc

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if response.status_code != 200:
            logger.warning(
                "Text provider rejected the request (status %s)",
                response.status_code,
                extra={
                    "event": "capture.provider.rejected",
                    "outcome": "upstream_error",
                    "provider": self.provider,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            raise TextProviderError("rejected", status_code=response.status_code)
        if len(response.content) > MAX_WIRE_BYTES:
            raise TextProviderError("oversized")

        try:
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or len(content) > MAX_COMPLETION_CHARS:
                raise ValueError("unusable completion")
            try:
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("completion is not an object")
            except ValueError:
                parsed = try_repair_json(content)
            check_json_depth(parsed)
            interpretation = RawInterpretation.model_validate(parsed)
        except Exception as exc:
            # The completion can contain the user's words: log only the failure type.
            logger.warning(
                "Text provider returned an unusable completion",
                extra={
                    "event": "capture.provider.invalid_completion",
                    "outcome": "upstream_error",
                    "provider": self.provider,
                    "error_type": type(exc).__name__,
                },
            )
            raise TextProviderError("invalid_completion") from exc

        logger.info(
            "Text provider interpreted a message",
            extra={
                "event": "capture.provider.completed",
                "outcome": "success",
                "provider": self.provider,
                "duration_ms": duration_ms,
            },
        )
        return interpretation


class FallbackTextGateway(TextCaptureGateway):
    """Tries each configured provider in order within one shared time budget."""

    def __init__(
        self,
        adapters: list[OpenAICompatibleTextAdapter],
        *,
        budget_seconds: float = TOTAL_BUDGET_SECONDS,
    ) -> None:
        self.adapters = adapters
        self.budget_seconds = budget_seconds

    async def interpret(
        self,
        *,
        context: CaptureContext,
        today: dt.date,
        original_text: str,
        follow_ups: list[str],
        previous: dict[str, Any] | None,
    ) -> RawInterpretation:
        if not self.adapters:
            raise TextProviderError("not_configured")
        deadline = time.monotonic() + self.budget_seconds
        last_error: TextProviderError | None = None
        for adapter in self.adapters:
            remaining = deadline - time.monotonic()
            if remaining < MIN_ATTEMPT_SECONDS:
                break
            try:
                return await adapter.interpret(
                    context=context,
                    today=today,
                    original_text=original_text,
                    follow_ups=follow_ups,
                    previous=previous,
                    timeout=remaining,
                )
            except TextProviderError as exc:
                last_error = exc
                logger.warning(
                    "Text provider failed; trying the next one if any",
                    extra={
                        "event": "capture.provider.failed",
                        "outcome": "retry",
                        "provider": adapter.provider,
                        "error_type": exc.kind,
                    },
                )
        raise last_error or TextProviderError("timeout")


def get_text_gateway(settings: Settings | None = None) -> TextCaptureGateway:
    """Builds the provider chain from settings: the selected provider first, the other as backup."""
    cfg = settings or get_settings()
    deepseek_key = cfg.deepseek_api_key.get_secret_value() if cfg.deepseek_api_key else ""
    gemini_key = cfg.gemini_api_key.get_secret_value() if cfg.gemini_api_key else ""
    deepseek = OpenAICompatibleTextAdapter(
        provider="deepseek",
        base_url=cfg.deepseek_base_url,
        api_key=deepseek_key,
        model=cfg.deepseek_model,
        max_tokens=min(cfg.llm_max_output_tokens, 1_024),
        # Reasoning only adds latency to a short extraction.
        extra_body={"thinking": {"type": "disabled"}},
    )
    gemini = OpenAICompatibleTextAdapter(
        provider="gemini",
        base_url=cfg.gemini_base_url,
        api_key=gemini_key,
        model=cfg.gemini_model,
        max_tokens=min(cfg.llm_max_output_tokens, 1_024),
    )
    ordered = [deepseek, gemini] if cfg.llm_provider == "deepseek" else [gemini, deepseek]
    configured = [adapter for adapter in ordered if adapter.api_key]
    return FallbackTextGateway(
        configured, budget_seconds=min(cfg.llm_timeout_seconds, TOTAL_BUDGET_SECONDS)
    )
