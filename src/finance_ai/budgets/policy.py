from decimal import Decimal

from finance_ai.budgets.periods import BudgetPeriod, ScheduleType
from finance_ai.budgets.schemas import BudgetScheduleCommand
from finance_ai.core.errors import RequestInvalidError

MONEY_MAX = Decimal("9999999999999999.99")


class BudgetPolicy:
    @staticmethod
    def validate_amount(value: Decimal) -> Decimal:
        if value <= 0 or value > MONEY_MAX:
            raise RequestInvalidError({"amount": [f"Amount must be between 0.01 and {MONEY_MAX}."]})
        return value

    @staticmethod
    def normalize_currency_code(value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise RequestInvalidError({"currency_code": ["Select a supported currency."]})
        return normalized

    @staticmethod
    def validate_period(value: BudgetPeriod) -> BudgetPeriod:
        return value

    @staticmethod
    def normalize_name(value: str | None, *, fallback: str) -> str:
        normalized = "" if value is None else " ".join(value.split())
        if not normalized:
            normalized = fallback
        if len(normalized) > 120:
            raise RequestInvalidError({"name": ["Name must contain at most 120 characters."]})
        return normalized

    @staticmethod
    def validate_schedule(value: BudgetScheduleCommand) -> BudgetScheduleCommand:
        if value.ends_on is not None and value.ends_on < value.starts_on:
            raise RequestInvalidError({"ends_on": ["End date cannot be before start date."]})
        if value.schedule_type is ScheduleType.RECURRING:
            if value.frequency_unit is None or value.frequency_interval is None:
                raise RequestInvalidError(
                    {"frequency_interval": ["Recurring budgets require a frequency."]}
                )
            if value.frequency_interval < 1 or value.frequency_interval > 999:
                raise RequestInvalidError(
                    {"frequency_interval": ["Frequency must be between 1 and 999."]}
                )
        elif value.frequency_unit is not None or value.frequency_interval is not None:
            raise RequestInvalidError(
                {"schedule_type": ["One-time budgets cannot include a recurring frequency."]}
            )
        return value
