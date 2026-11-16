"""Decides when a budget deserves an email (EML-03). It only decides: delivery stays in the
dispatcher, so another channel could reuse these rules.

``evaluate`` runs inside the transaction of the expense that may have changed the picture. It is
state based and idempotent: for every budget whose current period contains today it finds the
highest threshold (80 % or 100 % of the limit) that the spending has reached and records it once.
The outbox's unique key is ``budget:{id}:{period start}:{threshold}:{limit}``, so

* repeating the evaluation, or two requests racing, never produces a second email;
* a new period starts again from zero (different period start);
* changing the limit re-arms the alert (different limit);
* spending that falls back and rises again within a period does not repeat an alert.

Jumping past both thresholds with one expense sends only the higher one; the lower threshold is
recorded as skipped (and a still-waiting lower email is withdrawn) so it is never sent late.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from finance_ai.budgets.periods import PeriodRange, resolve_period
from finance_ai.budgets.repositories import BudgetRecord, DailySpendingRecord
from finance_ai.budgets.service import spent_in_period
from finance_ai.core.logging import get_logger
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.notifications import NotificationOutbox, NotificationPreference
from finance_ai.db.repositories.budgets import SQLAlchemyBudgetRepository
from finance_ai.notifications.outbox import enqueue

logger = get_logger(__name__)

THRESHOLDS = (80, 100)

Today = Callable[[], date]


def _utc_today() -> date:
    return datetime.now(UTC).date()


def dedupe_key(budget: BudgetRecord, period_start: date, threshold: int) -> str:
    return f"budget:{budget.id}:{period_start.isoformat()}:{threshold}:{budget.amount}"


@dataclass(frozen=True, slots=True)
class DueAlert:
    record: BudgetRecord
    period: PeriodRange
    spent: Decimal
    threshold: int  # the highest one reached: the only one worth an email
    superseded: tuple[int, ...]  # lower ones reached by the same spending: recorded, never sent


def current_periods(
    records: Sequence[BudgetRecord], today: date
) -> list[tuple[BudgetRecord, PeriodRange]]:
    """Active budgets paired with the period that contains ``today`` (budgets that have not
    started or are already over have nothing to warn about)."""
    current: list[tuple[BudgetRecord, PeriodRange]] = []
    for record in records:
        if not record.is_active:
            continue
        period = resolve_period(
            schedule_type=record.schedule_type,
            frequency_unit=record.frequency_unit,
            frequency_interval=record.frequency_interval,
            starts_on=record.starts_on,
            ends_on=record.ends_on,
            as_of=today,
        )
        if period.starts_on <= today <= period.ends_on:
            current.append((record, period))
    return current


def due_alerts(
    current: Sequence[tuple[BudgetRecord, PeriodRange]],
    spending: Sequence[DailySpendingRecord],
) -> list[DueAlert]:
    due: list[DueAlert] = []
    for record, period in current:
        spent = spent_in_period(record, period, spending)
        reached = [t for t in THRESHOLDS if spent * 100 >= record.amount * t]
        if reached:
            top = max(reached)
            due.append(
                DueAlert(
                    record=record,
                    period=period,
                    spent=spent,
                    threshold=top,
                    superseded=tuple(t for t in reached if t < top),
                )
            )
    return due


class BudgetAlertEvaluator:
    def __init__(self, session: Session, *, today: Today = _utc_today) -> None:
        self._session = session
        self._today = today

    def evaluate(self, user_id: int) -> int:
        """Record the alerts this person is due. Returns how many new emails were queued."""
        if not self._opted_in(user_id):
            return 0
        repository = SQLAlchemyBudgetRepository(self._session)
        current = current_periods(repository.list_owned(owner_id=user_id), self._today())
        if not current:
            return 0
        spending = repository.daily_spending(
            owner_id=user_id,
            starts_on=min(period.starts_on for _, period in current),
            ends_on=max(period.ends_on for _, period in current),
        )
        queued = 0
        for due in due_alerts(current, spending):
            record, start = due.record, due.period.starts_on
            payload = self._payload(record, start, due.period.ends_on, due.threshold, due.spent)
            if enqueue(
                self._session,
                user_id=user_id,
                kind="budget_alert",
                dedupe_key=dedupe_key(record, start, due.threshold),
                payload=payload,
            ):
                queued += 1
            for lower in due.superseded:
                lower_key = dedupe_key(record, start, lower)
                enqueue(
                    self._session,
                    user_id=user_id,
                    kind="budget_alert",
                    dedupe_key=lower_key,
                    payload=payload | {"threshold": lower},
                    status="skipped",
                    reason="superseded",
                )
                self._withdraw_waiting(lower_key)
        if queued:
            logger.info(
                "Budget alerts queued",
                extra={"event": "budget_alert.evaluate", "outcome": f"queued:{queued}"},
            )
        return queued

    def _opted_in(self, user_id: int) -> bool:
        """Also takes this person's preference row lock until the transaction ends, so two
        expenses saved at the same moment evaluate one after the other: the later one sees the
        earlier one's committed spending and no crossing is missed."""
        row = self._session.execute(
            select(
                NotificationPreference.email_enabled,
                NotificationPreference.budget_alerts,
                AppUser.email_verified_at,
                AppUser.is_active,
            )
            .join(AppUser, AppUser.id == NotificationPreference.user_id)
            .where(NotificationPreference.user_id == user_id)
            .with_for_update(of=NotificationPreference)
        ).one_or_none()
        return bool(
            row is not None
            and row.email_enabled
            and row.budget_alerts
            and row.email_verified_at is not None
            and row.is_active
        )

    def _withdraw_waiting(self, key: str) -> None:
        self._session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.dedupe_key == key, NotificationOutbox.status == "pending")
            .values(status="skipped", last_error_code="superseded")
        )

    @staticmethod
    def _payload(
        record: BudgetRecord, start: date, end: date, threshold: int, spent: Decimal
    ) -> dict[str, Any]:
        return {
            "budget_id": record.id,
            "budget_name": record.name,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "threshold": threshold,
            "spent": str(spent),
            "limit": str(record.amount),
            "currency_code": record.currency.code,
        }
