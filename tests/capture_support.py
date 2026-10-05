"""Shared fakes for the HU-013 text capture tests (no network, no PostgreSQL)."""

from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.capture.context import AccountEntry, CaptureContext, CatalogEntry
from finance_ai.capture.gateway import TextCaptureGateway
from finance_ai.capture.interpretation import RawInterpretation
from finance_ai.capture.service import TextCaptureService

CLIENT_NOW = datetime(2026, 10, 2, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))

ACCOUNTS = (
    AccountEntry(id=1, name="Efectivo", currency_code="COP"),
    AccountEntry(id=2, name="Nequi", currency_code="COP"),
    AccountEntry(id=3, name="Bancolombia", currency_code="COP"),
    AccountEntry(id=4, name="Dólares", currency_code="USD"),
)
CONTEXT = CaptureContext(
    accounts=ACCOUNTS,
    categories=(CatalogEntry(id=10, name="Comida"), CatalogEntry(id=11, name="Transporte")),
    income_sources=(CatalogEntry(id=20, name="Salario"),),
)


def raw(**fields: Any) -> RawInterpretation:
    """A model answer; confidence defaults to a confident one."""
    fields.setdefault("confidence", 0.95)
    return RawInterpretation.model_validate(fields)


class FakeGateway(TextCaptureGateway):
    """Returns queued interpretations (or raises queued errors) and records each call."""

    def __init__(self, *answers: RawInterpretation | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def interpret(self, **kwargs: Any) -> RawInterpretation:
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeExpenses:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    def create(self, *, owner_id: int, command: Any) -> Any:
        self.commands.append((owner_id, command))
        return SimpleNamespace(
            id=700 + len(self.commands),
            currency=SimpleNamespace(code="COP"),
            store=command.merchant,
            account_name="Efectivo",
        )


class FakeIncomes:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def create(self, *, owner_id: int, request: Any, capture_source: str = "manual") -> Any:
        self.requests.append((owner_id, request, capture_source))
        return SimpleNamespace(
            id=800 + len(self.requests),
            amount=request.amount,
            currency_code="COP",
            date=request.date,
            description=request.description,
            account_name="Nequi",
        )


class FakeTransfers:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def create(self, *, owner_id: int, request: Any, capture_source: str = "manual") -> Any:
        self.requests.append((owner_id, request, capture_source))
        return SimpleNamespace(
            id=900 + len(self.requests),
            source_account=SimpleNamespace(name="Nequi", currency_code="COP"),
            destination_account=SimpleNamespace(name="Bancolombia", currency_code="COP"),
            source_amount=request.source_amount,
            date=request.date,
            description=request.description,
        )


def build_service(
    session_factory: sessionmaker[Session],
    gateway: TextCaptureGateway,
    *,
    context: CaptureContext = CONTEXT,
    clock: Any = lambda: datetime.now(UTC),
) -> tuple[TextCaptureService, FakeExpenses, FakeIncomes, FakeTransfers]:
    expenses, incomes, transfers = FakeExpenses(), FakeIncomes(), FakeTransfers()
    service = TextCaptureService(
        session_factory=session_factory,
        gateway=gateway,
        context_loader=lambda _owner_id: context,
        expenses=expenses,  # type: ignore[arg-type]
        incomes=incomes,  # type: ignore[arg-type]
        transfers=transfers,  # type: ignore[arg-type]
        clock=clock,
    )
    return service, expenses, incomes, transfers


def amount(value: str) -> Decimal:
    return Decimal(value)
