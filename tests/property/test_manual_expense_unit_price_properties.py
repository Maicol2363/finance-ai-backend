from decimal import Decimal

from hypothesis import given, seed
from hypothesis import strategies as st

from finance_ai.expenses.service import manual_unit_price_from_total


@st.composite
def manual_expense_amount_and_quantity(draw: st.DrawFn) -> tuple[Decimal, Decimal]:
    cents = draw(st.integers(min_value=1, max_value=100_000_000))
    quantity_millis = draw(st.integers(min_value=1, max_value=100_000))
    return Decimal(cents) / Decimal("100"), Decimal(quantity_millis) / Decimal("1000")


@seed(20261027)
@given(case=manual_expense_amount_and_quantity())
def test_manual_unit_price_is_cent_precision_and_within_rounding_tolerance(
    case: tuple[Decimal, Decimal],
) -> None:
    total, quantity = case

    unit_price = manual_unit_price_from_total(total=total, quantity=quantity)

    assert unit_price == unit_price.quantize(Decimal("0.01"))
    assert abs(unit_price * quantity - total) <= quantity * Decimal("0.005")
