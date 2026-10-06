from decimal import Decimal, InvalidOperation

from finance_ai.accounts.schemas import AccountType, AccountUpdateCommand
from finance_ai.core.errors import RequestInvalidError

MONEY_QUANTUM = Decimal("0.01")
MONEY_MAX = Decimal("9999999999999999.99")


class AccountPolicy:
    def normalize_name(self, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 100:
            raise RequestInvalidError({"name": ["Use a name between 1 and 100 characters."]})
        return normalized

    def normalize_currency_code(self, value: str) -> str:
        normalized = value.strip().upper()
        if not 3 <= len(normalized) <= 10 or not normalized.isascii() or not normalized.isalpha():
            raise RequestInvalidError({"currency_code": ["Select a supported currency."]})
        return normalized

    def validate_account_type(self, value: AccountType) -> AccountType:
        try:
            return AccountType(value)
        except ValueError as exc:
            raise RequestInvalidError({"type": ["Select a supported account type."]}) from exc

    def validate_opening_balance(self, value: Decimal) -> Decimal:
        if not value.is_finite() or abs(value) > MONEY_MAX:
            raise RequestInvalidError(
                {"opening_balance": ["Use an amount within the supported range."]}
            )
        try:
            quantized = value.quantize(MONEY_QUANTUM)
        except InvalidOperation as exc:
            raise RequestInvalidError(
                {"opening_balance": ["Use an amount with at most two decimal places."]}
            ) from exc
        if quantized != value:
            raise RequestInvalidError(
                {"opening_balance": ["Use an amount with at most two decimal places."]}
            )
        return quantized

    def validate_update(self, command: AccountUpdateCommand) -> None:
        if all(
            value is None
            for value in (
                command.name,
                command.account_type,
                command.currency_code,
                command.opening_balance,
            )
        ):
            raise RequestInvalidError({"request": ["Provide at least one field to update."]})
