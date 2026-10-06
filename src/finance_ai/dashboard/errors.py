from __future__ import annotations

from finance_ai.core.errors import AppError


class DashboardCurrencyInvalidError(AppError):
    def __init__(self, currency_code: str) -> None:
        super().__init__(
            status=422,
            code="dashboard_currency_invalid",
            title="Currency is unavailable",
            detail="Select a currently supported currency.",
            field_errors={"currency": [f"Currency '{currency_code}' is unavailable."]},
        )


class DashboardDateRangeInvalidError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            status=422,
            code="dashboard_date_range_invalid",
            title="Date range is invalid",
            detail=detail,
            field_errors={"date_range": [detail]},
        )
