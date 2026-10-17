from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from finance_ai.core.logging import get_logger

logger = get_logger(__name__)


class ProblemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str
    correlation_id: str
    field_errors: dict[str, list[str]] | None = None


class AppError(Exception):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        field_errors: Mapping[str, list[str]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.field_errors = dict(field_errors) if field_errors else None
        self.headers = dict(headers) if headers else None


class RegistrationValidationError(AppError):
    def __init__(self, field_errors: Mapping[str, list[str]]) -> None:
        super().__init__(
            status=422,
            code="registration_invalid",
            title="Registration details are invalid",
            detail="Please correct the highlighted fields and try again.",
            field_errors=field_errors,
        )


class EmailAlreadyRegisteredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="email_already_registered",
            title="Email is already registered",
            detail="An account already uses this email. Try logging in instead.",
            field_errors={"email": ["This email is already registered."]},
        )


class RegistrationRateLimitedError(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status=429,
            code="registration_rate_limited",
            title="Too many registration attempts",
            detail="Please wait before trying again.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class RequestInvalidError(AppError):
    def __init__(self, field_errors: Mapping[str, list[str]]) -> None:
        super().__init__(
            status=422,
            code="request_invalid",
            title="Request details are invalid",
            detail="Please correct the highlighted fields and try again.",
            field_errors=field_errors,
        )


class InvalidCredentialsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=401,
            code="invalid_credentials",
            title="Login failed",
            detail="Invalid email or password.",
        )


class LoginRateLimitedError(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status=429,
            code="login_rate_limited",
            title="Too many login attempts",
            detail="Please wait before trying to log in again.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class SessionInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=401,
            code="session_invalid",
            title="Session is no longer valid",
            detail="Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class AuthenticationRequiredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=401,
            code="authentication_required",
            title="Authentication required",
            detail="Please log in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ServiceUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=503,
            code="service_unavailable",
            title="Service temporarily unavailable",
            detail="The service is temporarily unavailable. Please try again later.",
        )


class AccountNameConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="account_name_conflict",
            title="Account name is already in use",
            detail="Choose a different name for this active account.",
            field_errors={"name": ["An active account already uses this name."]},
        )


class AccountCurrencyInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="account_currency_invalid",
            title="Currency is unavailable",
            detail="Select a currently supported currency.",
            field_errors={"currency_code": ["Select a currently supported currency."]},
        )


class AccountNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="account_not_found",
            title="Account not found",
            detail="The requested account is unavailable.",
        )


class AccountCurrencyLockedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="account_currency_locked",
            title="Account currency cannot be changed",
            detail="Currency cannot change after confirmed activity exists.",
            field_errors={"currency_code": ["Currency is locked for this account."]},
        )


class AccountReactivationConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="account_reactivation_conflict",
            title="Account cannot be reactivated",
            detail="Update the account details before reactivating it.",
        )


class AccountDeleteBlockedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="account_delete_blocked",
            title="Account cannot be permanently deleted",
            detail="This account has financial activity. Deactivate it to preserve its history.",
        )


class CategoryNameConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="category_name_conflict",
            title="Category name is already in use",
            detail="Choose a different category name.",
            field_errors={"name": ["A category already uses this name."]},
        )


class CategoryNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="category_not_found",
            title="Category not found",
            detail="The requested category is unavailable.",
        )


class CategoryAppearanceInvalidError(AppError):
    def __init__(self, field: str) -> None:
        super().__init__(
            status=422,
            code="category_appearance_invalid",
            title="Category appearance is invalid",
            detail="Choose a supported category appearance.",
            field_errors={field: [f"Select a supported {field}."]},
        )


class CategoryOrderInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="category_order_invalid",
            title="Category order is invalid",
            detail="The order must include each of your categories exactly once.",
            field_errors={"category_ids": ["Include each category exactly once."]},
        )


class BudgetNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="budget_not_found",
            title="Budget not found",
            detail="The requested budget is unavailable.",
        )


class BudgetCategoryUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="budget_category_unavailable",
            title="Budget category is unavailable",
            detail="Select one of your categories.",
            field_errors={"category_id": ["Select one of your categories."]},
        )


class BudgetCurrencyInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="budget_currency_invalid",
            title="Budget currency is unavailable",
            detail="Select a currently supported currency.",
            field_errors={"currency_code": ["Select a currently supported currency."]},
        )


class IncomeSourceValidationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            status=422,
            code="income_source_invalid",
            title="Income source is invalid",
            detail="Please correct the highlighted fields and try again.",
            field_errors={"name": [message]},
        )


class IncomeSourceNameTakenError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="income_source_name_taken",
            title="Income source name already exists",
            detail="You already have an income source with this name.",
            field_errors={"name": ["This name is already in use."]},
        )


class IncomeSourceNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="income_source_not_found",
            title="Income source not found",
            detail="This income source does not exist.",
        )


class ExpenseNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="expense_not_found",
            title="Expense not found",
            detail="The requested expense is unavailable.",
        )


class ReceiptProposalNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="receipt_proposal_not_found",
            title="Receipt proposal not found",
            detail="This receipt draft is no longer available. Scan the receipt again.",
        )


class ExpenseNotEditableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="expense_not_editable",
            title="Expense cannot be edited",
            detail="Only confirmed expenses can be edited or deleted.",
        )


class ExpenseAccountInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="expense_account_invalid",
            title="Expense account is invalid",
            detail="Select an active account with the expense currency.",
            field_errors={"account_id": ["Select an active account with the expense currency."]},
        )


class ExpenseCategoryNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="expense_category_not_found",
            title="Expense category is unavailable",
            detail="Select one of your categories or leave the expense uncategorized.",
            field_errors={"category_id": ["Select one of your categories."]},
        )


class HistoryFilterInvalidError(AppError):
    def __init__(self, field_errors: Mapping[str, list[str]]) -> None:
        super().__init__(
            status=422,
            code="history_filter_invalid",
            title="History filters are invalid",
            detail="Please correct the selected history filters.",
            field_errors=field_errors,
        )


class IncomeNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="income_not_found",
            title="Income not found",
            detail="The requested income is unavailable.",
        )


class IncomeAccountUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="income_account_unavailable",
            title="Account is unavailable",
            detail="Choose one of your active accounts.",
            field_errors={"account_id": ["Choose one of your active accounts."]},
        )


class IncomeSourceUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="income_source_unavailable",
            title="Income source is unavailable",
            detail="Choose one of your income sources or leave it empty.",
            field_errors={"income_source_id": ["Choose one of your income sources."]},
        )


class TransferAccountUnavailableError(AppError):
    def __init__(self, field: str) -> None:
        super().__init__(
            status=422,
            code="transfer_account_unavailable",
            title="Account is unavailable",
            detail="Choose one of your active accounts.",
            field_errors={field: ["Choose one of your active accounts."]},
        )


class TransferSameAccountError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="transfer_same_account",
            title="Accounts must differ",
            detail="Choose two different accounts for the transfer.",
            field_errors={
                "destination_account_id": ["Choose a different account from the source."]
            },
        )


class TransferDestinationAmountRequiredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="transfer_destination_amount_required",
            title="Destination amount is required",
            detail="Declare the amount arriving in the destination currency.",
            field_errors={"destination_amount": ["Declare the arriving destination amount."]},
        )


class TransferSourceDestinationMismatchError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="transfer_source_destination_mismatch",
            title="Transfer amounts must match",
            detail="Same-currency transfers move the same amount on both sides.",
            field_errors={"destination_amount": ["Use the same amount as the source."]},
        )


class TransferInsufficientFundsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            code="transfer_insufficient_funds",
            title="Insufficient funds",
            detail="The source account balance cannot cover this transfer.",
            field_errors={"source_amount": ["Use an amount within the source balance."]},
        )


class TextProposalNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=404,
            code="text_proposal_not_found",
            title="Text proposal not found",
            detail="This draft is no longer available. Write your message again.",
        )


class TextProposalClosedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="text_proposal_closed",
            title="Text proposal is closed",
            detail="This draft was already discarded. Write your message again.",
        )


class TextCaptureUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=503,
            code="text_capture_unavailable",
            title="Text capture is unavailable",
            detail="The assistant could not read your message now. Retry or use the manual form.",
        )


class TextCaptureNotConfiguredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=503,
            code="text_capture_not_configured",
            title="Text capture is not configured",
            detail="The AI assistant is not set up on the server. Use the manual form.",
        )


class TextCaptureRateLimitedError(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status=429,
            code="text_capture_rate_limited",
            title="Too many messages",
            detail="Please wait a moment before sending another message.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


def correlation_id_from(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unavailable"))


def problem_payload(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    correlation_id: str,
    field_errors: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return ProblemResponse(
        title=title,
        status=status,
        code=code,
        detail=detail,
        correlation_id=correlation_id,
        field_errors=field_errors,
    ).model_dump(exclude_none=True)


def _validation_field_errors(exc: RequestValidationError) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for item in exc.errors():
        location = item.get("loc", ())
        field = str(location[-1]) if location else "request"
        message = str(item.get("msg", "Invalid value"))
        errors.setdefault(field, []).append(message)
    return errors


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            headers=exc.headers,
            content=problem_payload(
                status=exc.status,
                code=exc.code,
                title=exc.title,
                detail=exc.detail,
                correlation_id=correlation_id_from(request),
                field_errors=exc.field_errors,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path == "/api/v1/auth/register":
            error: AppError = RegistrationValidationError(_validation_field_errors(exc))
        else:
            error = RequestInvalidError(_validation_field_errors(exc))
        return await app_error_handler(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code = "route_not_found"
            title = "Resource not found"
            detail = "The requested resource is unavailable."
        elif exc.status_code == 405:
            code = "method_not_allowed"
            title = "Method not allowed"
            detail = "This request method is not supported for the resource."
        else:
            code = "http_error"
            title = "Request failed"
            detail = "The request could not be completed."
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=problem_payload(
                status=exc.status_code,
                code=code,
                title=title,
                detail=detail,
                correlation_id=correlation_id_from(request),
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled request failure",
            extra={
                "correlation_id": correlation_id_from(request),
                "event": "request.unhandled_failure",
                "outcome": "internal_error",
                "status": 500,
            },
        )
        return JSONResponse(
            status_code=500,
            headers={"X-Correlation-ID": correlation_id_from(request)},
            content=problem_payload(
                status=500,
                code="internal_error",
                title="Unexpected error",
                detail="Something went wrong. Please try again later.",
                correlation_id=correlation_id_from(request),
            ),
        )
