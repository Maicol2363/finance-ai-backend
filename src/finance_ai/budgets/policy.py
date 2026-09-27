from decimal import Decimal

from finance_ai.budgets.periods import BudgetPeriod
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
