from __future__ import annotations

import pytest

from scripts import benchmark_staff_session


class FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, path: str, **kwargs: object) -> FakeResponse:
        self.calls.append((path, kwargs))
        return self.responses.pop(0)


@pytest.mark.anyio
async def test_staff_benchmark_waits_for_retry_after_during_login_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def record_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(benchmark_staff_session.asyncio, "sleep", record_wait)
    client = FakeClient([FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200)])

    await benchmark_staff_session._login_staff_account(
        client, "agent@example.test", "test-password", "http://127.0.0.1:5173"
    )

    assert waits == [7.1]
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]


@pytest.mark.anyio
async def test_staff_benchmark_stops_after_non_rate_limit_login_failure() -> None:
    client = FakeClient([FakeResponse(503)])

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await benchmark_staff_session._login_staff_account(
            client, "agent@example.test", "test-password", "http://127.0.0.1:5173"
        )

    assert len(client.calls) == 1
