from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from finance_ai.budgets.periods import (
    PeriodRange,
    ScheduleType,
    default_start,
    period_frequency,
    resolve_period,
)
from finance_ai.budgets.policy import BudgetPolicy
from finance_ai.budgets.repositories import BudgetCategoryRecord, BudgetRecord, DailySpendingRecord
from finance_ai.budgets.schemas import (
    BudgetCategoryView,
    BudgetCreateCommand,
    BudgetListResult,
    BudgetUpdateCommand,
    BudgetView,
)
from finance_ai.budgets.uow import BudgetUnitOfWork
from finance_ai.core.errors import (
    BudgetCategoryUnavailableError,
    BudgetCurrencyInvalidError,
    BudgetNotFoundError,
    ServiceUnavailableError,
)

BudgetUnitOfWorkFactory = Callable[[], BudgetUnitOfWork]
TodayFactory = Callable[[], date]


class BudgetService:
    def __init__(
        self,
        *,
        policy: BudgetPolicy,
        uow_factory: BudgetUnitOfWorkFactory,
        today_factory: TodayFactory | None = None,
    ) -> None:
        self.policy = policy
        self.uow_factory = uow_factory
        self.today_factory = today_factory or (lambda: datetime.now(UTC).date())

    def create(self, *, owner_id: int, command: BudgetCreateCommand) -> BudgetView:
        amount = self.policy.validate_amount(command.amount)
        currency_code = self.policy.normalize_currency_code(command.currency_code)
        period = self.policy.validate_period(command.period)
        today = self.today_factory()

        with self.uow_factory() as uow:
            category = uow.budgets.find_category_owned(
                owner_id=owner_id, category_id=command.category_id
            )
            if category is None:
                raise BudgetCategoryUnavailableError
            currency = uow.currencies.find_active_by_code(currency_code)
            if currency is None:
                raise BudgetCurrencyInvalidError
            budget_id = uow.budgets.add(
                owner_id=owner_id,
                category_id=category.id,
                currency_id=currency.id,
                name=category.name,
                amount=amount,
                schedule_type=ScheduleType.RECURRING,
                frequency_unit=period_frequency(period),
                frequency_interval=1,
                starts_on=default_start(period, today),
            )
            uow.flush()
            created = self._require_owned(uow, owner_id=owner_id, budget_id=budget_id)
            view = self._views(uow, records=[created], as_of=today)[0]
            uow.commit()
            return view

    def list(self, *, owner_id: int, preferred_currency_code: str) -> BudgetListResult:
        today = self.today_factory()
        with self.uow_factory() as uow:
            records = uow.budgets.list_owned(owner_id=owner_id)
            categories = uow.budgets.list_categories(owner_id=owner_id)
            currencies = uow.currencies.list_active()
            suggestion = uow.currencies.first_active_code((preferred_currency_code.upper(), "COP"))
            if suggestion is None and currencies:
                suggestion = currencies[0].code
            if suggestion is None:
                raise ServiceUnavailableError
            views = self._views(uow, records=records, as_of=today)
        return BudgetListResult(
            budgets=tuple(views),
            categories=tuple(self._category_view(item) for item in categories),
            supported_currencies=tuple(currencies),
            suggested_currency_code=suggestion,
        )

    def get(self, *, owner_id: int, budget_id: int) -> BudgetView:
        today = self.today_factory()
        with self.uow_factory() as uow:
            record = self._require_owned(uow, owner_id=owner_id, budget_id=budget_id)
            return self._views(uow, records=[record], as_of=today)[0]

    def update(self, *, owner_id: int, budget_id: int, command: BudgetUpdateCommand) -> BudgetView:
        today = self.today_factory()
        amount = None if command.amount is None else self.policy.validate_amount(command.amount)
        period = None if command.period is None else self.policy.validate_period(command.period)
        currency_code = (
            None
            if command.currency_code is None
            else self.policy.normalize_currency_code(command.currency_code)
        )

        with self.uow_factory() as uow:
            self._require_owned(
                uow,
                owner_id=owner_id,
                budget_id=budget_id,
                for_update=True,
            )
            category_id: int | None = None
            name: str | None = None
            if command.category_id is not None:
                category = uow.budgets.find_category_owned(
                    owner_id=owner_id, category_id=command.category_id
                )
                if category is None:
                    raise BudgetCategoryUnavailableError
                category_id = category.id
                name = category.name
            currency_id: int | None = None
            if currency_code is not None:
                currency = uow.currencies.find_active_by_code(currency_code)
                if currency is None:
                    raise BudgetCurrencyInvalidError
                currency_id = currency.id
            frequency_unit = None if period is None else period_frequency(period)
            starts_on = None if period is None else default_start(period, today)
            uow.budgets.update(
                owner_id=owner_id,
                budget_id=budget_id,
                category_id=category_id,
                currency_id=currency_id,
                name=name,
                amount=amount,
                frequency_unit=frequency_unit,
                starts_on=starts_on,
            )
            uow.flush()
            updated = self._require_owned(uow, owner_id=owner_id, budget_id=budget_id)
            view = self._views(uow, records=[updated], as_of=today)[0]
            uow.commit()
            return view

    def delete(self, *, owner_id: int, budget_id: int) -> None:
        with self.uow_factory() as uow:
            self._require_owned(
                uow,
                owner_id=owner_id,
                budget_id=budget_id,
                for_update=True,
            )
            uow.budgets.delete(owner_id=owner_id, budget_id=budget_id)
            uow.flush()
            uow.commit()

    @staticmethod
    def _require_owned(
        uow: BudgetUnitOfWork,
        *,
        owner_id: int,
        budget_id: int,
        for_update: bool = False,
    ) -> BudgetRecord:
        record = uow.budgets.find_owned(
            owner_id=owner_id,
            budget_id=budget_id,
            for_update=for_update,
        )
        if record is None:
            raise BudgetNotFoundError
        return record

    def _views(
        self,
        uow: BudgetUnitOfWork,
        *,
        records: Sequence[BudgetRecord],
        as_of: date,
    ) -> tuple[BudgetView, ...]:
        if not records:
            return ()
        periods = {
            record.id: resolve_period(
                schedule_type=record.schedule_type,
                frequency_unit=record.frequency_unit,
                frequency_interval=record.frequency_interval,
                starts_on=record.starts_on,
                ends_on=record.ends_on,
                as_of=as_of,
            )
            for record in records
        }
        spending = uow.budgets.daily_spending(
            owner_id=records[0].user_id,
            starts_on=min(item.starts_on for item in periods.values()),
            ends_on=max(item.ends_on for item in periods.values()),
        )
        return tuple(self._view(record, periods[record.id], spending) for record in records)

    @classmethod
    def _view(
        cls,
        record: BudgetRecord,
        period: PeriodRange,
        spending: Sequence[DailySpendingRecord],
    ) -> BudgetView:
        spent = sum(
            (
                item.amount
                for item in spending
                if item.currency_id == record.currency.id
                and period.starts_on <= item.spent_on <= period.ends_on
                and (record.category is None or item.category_id == record.category.id)
            ),
            Decimal("0"),
        )
        return BudgetView(
            id=record.id,
            category=(None if record.category is None else cls._category_view(record.category)),
            currency=record.currency,
            amount=record.amount,
            schedule_type=record.schedule_type,
            frequency_unit=record.frequency_unit,
            frequency_interval=record.frequency_interval,
            starts_on=record.starts_on,
            ends_on=record.ends_on,
            current_period_start=period.starts_on,
            current_period_end=period.ends_on,
            spent_amount=spent,
            is_active=record.is_active,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _category_view(record: BudgetCategoryRecord) -> BudgetCategoryView:
        return BudgetCategoryView(
            id=record.id,
            name=record.name,
            color=record.color,
            icon=record.icon,
        )
