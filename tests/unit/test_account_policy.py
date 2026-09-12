from decimal import Decimal

import pytest
from pydantic import ValidationError

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.schemas import (
    AccountCreateRequest,
    AccountType,
    AccountUpdateCommand,
    AccountUpdateRequest,
)
from finance_ai.core.errors import RequestInvalidError


def test_name_currency_type_and_signed_money_are_normalized() -> None:
    policy = AccountPolicy()

    assert policy.normalize_name("  Daily cash  ") == "Daily cash"
    assert policy.normalize_currency_code(" cop ") == "COP"
    assert policy.validate_account_type(AccountType.CREDIT_CARD) is AccountType.CREDIT_CARD
    assert policy.validate_opening_balance(Decimal("-1250.50")) == Decimal("-1250.50")


@pytest.mark.parametrize(
    "value",
    [Decimal("0.001"), Decimal("10000000000000000.00"), Decimal("NaN"), Decimal("Infinity")],
)
def test_money_outside_numeric_18_2_is_rejected(value: Decimal) -> None:
    with pytest.raises(RequestInvalidError) as captured:
        AccountPolicy().validate_opening_balance(value)

    assert captured.value.code == "request_invalid"
    assert set(captured.value.field_errors or {}) == {"opening_balance"}


@pytest.mark.parametrize("value", ["", "   ", "x" * 101])
def test_invalid_names_are_rejected_without_echoing_the_value(value: str) -> None:
    with pytest.raises(RequestInvalidError) as captured:
        AccountPolicy().normalize_name(value)

    assert captured.value.detail == "Please correct the highlighted fields and try again."


def test_api_models_reject_binary_float_extra_owner_and_empty_or_null_patch() -> None:
    with pytest.raises(ValidationError):
        AccountCreateRequest.model_validate(
            {
                "name": "Cash",
                "type": "cash",
                "currency_code": "COP",
                "opening_balance": 1.2,
                "user_id": 99,
            }
        )
    with pytest.raises(ValidationError):
        AccountUpdateRequest.model_validate({})
    with pytest.raises(ValidationError):
        AccountUpdateRequest.model_validate({"name": None})


def test_domain_policy_rejects_an_empty_update_independently_of_http() -> None:
    with pytest.raises(RequestInvalidError):
        AccountPolicy().validate_update(AccountUpdateCommand())
