"""Business rules for expense correction and unified history queries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from finance_ai.categories.schemas import CategoryColor, CategoryIcon
from finance_ai.core.errors import (
    ExpenseAccountInvalidError,
    ExpenseCategoryNotFoundError,
    ExpenseNotEditableError,
    ExpenseNotFoundError,
    HistoryFilterInvalidError,
    RequestInvalidError,
)
from finance_ai.expenses.repositories import ExpenseRecord, HistoryFilters
from finance_ai.expenses.schemas import (
    CreateExpenseCommand,
    ExpenseListResult,
    ExpenseUpdateCommand,
    HistoryPeriod,
    HistoryResult,
)
from finance_ai.expenses.uow import ExpenseUnitOfWork
from finance_ai.receipts.schemas import ReceiptProposalConfirmRequest

ExpenseUnitOfWorkFactory = Callable[[], ExpenseUnitOfWork]
# receipt.total and expense.amount are NUMERIC(12, 2) in the persisted schema.
MONEY_MAX = Decimal("9999999999.99")
# Proposal states a client may confirm. `failed_extraction` drafts are recoverable: when every
# provider attempt fails the user completes the required fields by hand (HU-015 provider-failure
# recovery), so the confirmation payload - not the extraction - is the source of truth.
CONFIRMABLE_PROPOSAL_STATUSES = frozenset({"complete", "partial", "failed_extraction"})
# Stored `financial_transaction.capture_source` values behind each filter group the
# app offers: the three AI capture paths collapse into `ai` (see `CaptureSource`
# in the Flutter client), so one chip can select all of them.
CAPTURE_SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "text_ai", "audio_ai", "receipt_photo_ai"),
    "manual": ("manual",),
    "import": ("import",),
}


class ExpenseService:
    """Coordinates HU-006 creation, HU-008 mutations, and HU-011 history reads in one UoW."""

    def __init__(self, *, uow_factory: ExpenseUnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

    def create(
        self,
        *,
        owner_id: int,
        command: CreateExpenseCommand,
    ) -> ExpenseRecord:
        if command.amount <= Decimal("0") or command.amount > MONEY_MAX:
            raise RequestInvalidError({"amount": [f"Amount must be between 0.01 and {MONEY_MAX}."]})
        if not command.merchant.strip():
            raise RequestInvalidError({"merchant": ["Merchant cannot be empty."]})

        with self.uow_factory() as uow:
            account_currency = uow.expenses.active_account_currency(
                owner_id=owner_id, account_id=command.account_id
            )
            if account_currency is None:
                raise ExpenseAccountInvalidError

            if command.category_id is not None and not uow.expenses.category_owned(
                owner_id=owner_id, category_id=command.category_id
            ):
                raise ExpenseCategoryNotFoundError

            created = uow.expenses.create_manual(
                owner_id=owner_id,
                account_id=command.account_id,
                amount=command.amount,
                merchant=command.merchant.strip(),
                occurred_at=command.occurred_at,
                category_id=command.category_id,
                note=command.note,
            )
            uow.flush()
            uow.commit()
            return created

    def confirm_receipt_proposal(
        self,
        *,
        owner_id: int,
        proposal_id: UUID,
        command: ReceiptProposalConfirmRequest,
    ) -> tuple[ExpenseRecord, bool]:
        """Validates proposal state, verifies account/currency, and executes atomic confirmation.

        Returns:
            A tuple of (ExpenseRecord, is_new), where is_new is False if the request was an
            idempotent replay of an already confirmed proposal.
        """
        if command.total_amount <= Decimal("0") or command.total_amount > MONEY_MAX:
            raise RequestInvalidError(
                {"total_amount": [f"Total amount must be between 0.01 and {MONEY_MAX}."]}
            )
        if not command.merchant.strip():
            raise RequestInvalidError({"merchant": ["Merchant cannot be empty."]})

        with self.uow_factory() as uow:
            # 1. Pessimistic lock on proposal to cleanly serialize concurrent confirmations
            proposal = uow.proposals.get_by_id_for_user(owner_id, proposal_id, for_update=True)
            if proposal is None:
                raise ExpenseNotFoundError

            # 2. Idempotency replay check
            if proposal.status == "confirmed":
                tx_id = proposal.proposal_data.get("transaction_id")
                if tx_id is not None:
                    existing = uow.expenses.find_by_transaction_id(
                        owner_id=owner_id, transaction_id=int(tx_id)
                    )
                    if existing is not None:
                        return existing, False

            # 3. State validations
            if proposal.status == "discarded":
                raise RequestInvalidError(
                    {"proposal": ["This receipt proposal was previously discarded."]}
                )
            if proposal.status == "not_a_receipt":
                raise RequestInvalidError(
                    {
                        "proposal": [
                            "This image was classified as not a receipt and cannot be confirmed."
                        ]
                    }
                )
            if proposal.status not in CONFIRMABLE_PROPOSAL_STATUSES:
                raise RequestInvalidError(
                    {"proposal": [f"Cannot confirm proposal with status '{proposal.status}'."]}
                )

            # 4. Expiration check
            now_utc = datetime.now(UTC)
            expires_at = proposal.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if now_utc > expires_at:
                raise RequestInvalidError(
                    {
                        "proposal": [
                            "The receipt proposal has expired. Please scan the receipt again."
                        ]
                    }
                )

            # 5. Account and currency verification
            account_currency = uow.expenses.active_account_currency(
                owner_id=owner_id, account_id=command.account_id
            )
            if (
                account_currency is None
                or account_currency.code.upper() != command.currency.upper()
            ):
                raise ExpenseAccountInvalidError

            # 6. Category verification & JIT creation
            category_id = command.category_id
            if category_id is None and command.new_category_name:
                cleaned_name = command.new_category_name.strip()
                if cleaned_name and hasattr(uow, "categories"):
                    existing_cats = uow.categories.list_owned(owner_id=owner_id)
                    matched_cat = next(
                        (
                            c
                            for c in existing_cats
                            if c.name.strip().lower() == cleaned_name.lower()
                        ),
                        None,
                    )
                    if matched_cat is not None:
                        category_id = matched_cat.id
                    else:
                        category_id = uow.categories.add(
                            owner_id=owner_id,
                            name=cleaned_name,
                            color=CategoryColor.BLUE,
                            icon=CategoryIcon.OTHER,
                            sort_order=len(existing_cats),
                        )
                        uow.flush()

            if category_id is not None and not uow.expenses.category_owned(
                owner_id=owner_id, category_id=category_id
            ):
                raise ExpenseCategoryNotFoundError

            for item in command.items:
                if item.category_id is not None and not uow.expenses.category_owned(
                    owner_id=owner_id, category_id=item.category_id
                ):
                    raise ExpenseCategoryNotFoundError

            # 7. Relational multi-entity persistence
            created = uow.expenses.create_from_receipt_proposal(
                owner_id=owner_id,
                account_id=command.account_id,
                total_amount=command.total_amount,
                merchant=command.merchant.strip(),
                occurred_at=command.occurred_at,
                category_id=category_id,
                items=command.items,
                note=command.note,
            )

            # 8. Mark proposal confirmed with transaction_id
            uow.proposals.mark_confirmed(
                user_id=owner_id,
                proposal_id=proposal_id,
                transaction_id=created.transaction_id,
            )

            uow.flush()
            uow.commit()
            return created, True

    def list(self, *, owner_id: int, page: int, page_size: int) -> ExpenseListResult:
        if page < 1 or page_size < 1 or page_size > 100:
            raise HistoryFilterInvalidError(
                {"page": ["Page must be positive."]}
                if page < 1
                else {"page_size": ["Page size must be between 1 and 100."]}
            )
        with self.uow_factory() as uow:
            items, total = uow.expenses.list_owned(
                owner_id=owner_id,
                offset=(page - 1) * page_size,
                limit=page_size,
            )
        return ExpenseListResult(items=tuple(items), total=total, page=page, page_size=page_size)

    def get(self, *, owner_id: int, expense_id: int) -> ExpenseRecord:
        with self.uow_factory() as uow:
            expense = uow.expenses.find_owned(owner_id=owner_id, expense_id=expense_id)
        if expense is None:
            raise ExpenseNotFoundError
        return expense

    def find_potential_duplicate(
        self,
        *,
        owner_id: int,
        merchant: str,
        total_amount: Decimal,
        occurred_date: date,
    ) -> bool:
        with self.uow_factory() as uow:
            return uow.expenses.find_potential_duplicate(
                owner_id=owner_id,
                merchant=merchant,
                total_amount=total_amount,
                occurred_date=occurred_date,
            )

    def update(
        self,
        *,
        owner_id: int,
        expense_id: int,
        command: ExpenseUpdateCommand,
    ) -> ExpenseRecord:
        self._validate_command(command)
        items_provided = "items" in command.fields
        with self.uow_factory() as uow:
            current = uow.expenses.find_owned(
                owner_id=owner_id,
                expense_id=expense_id,
                for_update=True,
            )
            current = self._require_editable(current)

            if (
                "category_id" in command.fields
                and command.category_id is not None
                and not uow.expenses.category_owned(
                    owner_id=owner_id, category_id=command.category_id
                )
            ):
                raise ExpenseCategoryNotFoundError

            if items_provided:
                for item_category_id in {
                    item.category_id for item in command.items or () if item.category_id is not None
                }:
                    if not uow.expenses.category_owned(
                        owner_id=owner_id, category_id=item_category_id
                    ):
                        raise ExpenseCategoryNotFoundError

            account_id = current.account_id
            if "account_id" in command.fields:
                assert command.account_id is not None
                account_id = command.account_id
            if "account_id" in command.fields or (
                "amount" in command.fields and not items_provided
            ):
                currency = uow.expenses.active_account_currency(
                    owner_id=owner_id, account_id=account_id
                )
                if currency is None or currency.code != current.currency.code:
                    raise ExpenseAccountInvalidError

            # An submitted item list replaces the receipt's lines, and those lines
            # define the official total: the amount is not applied separately.
            applied_fields = (
                frozenset(field for field in command.fields if field not in {"items", "amount"})
                if items_provided
                else command.fields
            )
            uow.expenses.update(
                owner_id=owner_id,
                expense_id=expense_id,
                fields=applied_fields,
                name=command.name,
                amount=None if items_provided else command.amount,
                note=command.note,
                receipt_name=command.receipt_name,
                receipt_note=command.receipt_note,
                category_id=command.category_id,
                account_id=account_id if "account_id" in command.fields else None,
                occurred_at=command.occurred_at,
                description=command.description,
            )
            if items_provided:
                uow.expenses.replace_items(
                    owner_id=owner_id,
                    expense_id=expense_id,
                    items=command.items or (),
                    fallback_amount=command.amount,
                )
            uow.flush()
            updated = uow.expenses.find_owned(owner_id=owner_id, expense_id=expense_id)
            if updated is None:
                raise ExpenseNotFoundError
            uow.commit()
            return updated

    def delete(self, *, owner_id: int, expense_id: int) -> None:
        with self.uow_factory() as uow:
            current = uow.expenses.find_owned(
                owner_id=owner_id,
                expense_id=expense_id,
                for_update=True,
            )
            self._require_editable(current)
            uow.expenses.delete(owner_id=owner_id, expense_id=expense_id)
            uow.flush()
            uow.commit()

    def history(
        self,
        *,
        owner_id: int,
        search: str | None,
        transaction_type: str | None,
        category_id: int | None,
        account_id: int | None,
        period: HistoryPeriod | None,
        start_date: date | None,
        end_date: date | None,
        page: int,
        page_size: int,
        store: str | None = None,
        capture_source: str | None = None,
        receipt_kind: str | None = None,
    ) -> HistoryResult:
        if page < 1 or page_size < 1 or page_size > 100:
            raise HistoryFilterInvalidError(
                {"page": ["Page must be positive."]}
                if page < 1
                else {"page_size": ["Page size must be between 1 and 100."]}
            )
        if category_id is not None and category_id < 1:
            raise HistoryFilterInvalidError({"category_id": ["Category id must be positive."]})
        if account_id is not None and account_id < 1:
            raise HistoryFilterInvalidError({"account_id": ["Account id must be positive."]})
        if transaction_type is not None and transaction_type not in {
            "expense",
            "income",
            "transfer",
        }:
            raise HistoryFilterInvalidError(
                {"transaction_type": ["Transaction type is unsupported."]}
            )
        if capture_source is not None and capture_source not in CAPTURE_SOURCE_GROUPS:
            raise HistoryFilterInvalidError(
                {
                    "capture_source": [
                        "Capture source must be one of: "
                        f"{', '.join(sorted(CAPTURE_SOURCE_GROUPS))}."
                    ]
                }
            )
        if receipt_kind is not None and receipt_kind not in {"single", "multi"}:
            raise HistoryFilterInvalidError(
                {"receipt_kind": ["Receipt kind must be 'single' or 'multi'."]}
            )
        resolved_store = None if store is None or not store.strip() else store.strip()
        resolved_capture_sources: tuple[str, ...] = ()
        if capture_source is not None:
            resolved_capture_sources = CAPTURE_SOURCE_GROUPS.get(capture_source, ())

        resolved_start, resolved_end = self._resolve_period_dates(
            period=period,
            start_date=start_date,
            end_date=end_date,
        )
        filters = HistoryFilters(
            search=search,
            transaction_type=transaction_type,
            category_id=category_id,
            account_id=account_id,
            start_date=resolved_start,
            end_date=resolved_end,
            store=resolved_store,
            capture_sources=resolved_capture_sources,
            receipt_kind=receipt_kind,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        with self.uow_factory() as uow:
            items, total = uow.expenses.list_history(owner_id=owner_id, filters=filters)
        return HistoryResult(items=tuple(items), total=total, page=page, page_size=page_size)

    def _require_editable(self, expense: ExpenseRecord | None) -> ExpenseRecord:
        if expense is None:
            raise ExpenseNotFoundError
        if expense.status != "confirmed":
            raise ExpenseNotEditableError
        return expense

    def _validate_command(self, command: ExpenseUpdateCommand) -> None:
        errors: dict[str, list[str]] = {}
        if "name" in command.fields and (command.name is None or not command.name.strip()):
            errors["name"] = ["Expense name cannot be empty."]
        if "amount" in command.fields:
            if command.amount is None or command.amount <= Decimal("0"):
                errors["amount"] = ["Amount must be greater than zero."]
            elif command.amount > MONEY_MAX:
                errors["amount"] = [f"Amount cannot exceed {MONEY_MAX}."]
        if "receipt_name" in command.fields and (
            command.receipt_name is None or not command.receipt_name.strip()
        ):
            errors["receipt_name"] = ["Receipt name cannot be empty."]
        if "account_id" in command.fields and (
            command.account_id is None or command.account_id < 1
        ):
            errors["account_id"] = ["Account id must be positive."]
        if "category_id" in command.fields and (
            command.category_id is not None and command.category_id < 1
        ):
            errors["category_id"] = ["Category id must be positive."]
        if errors:
            raise RequestInvalidError(errors)

    def _resolve_period_dates(
        self,
        *,
        period: HistoryPeriod | None,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[date | None, date | None]:
        if period is None and start_date is None and end_date is None:
            return None, None
        if start_date is not None and end_date is not None and start_date > end_date:
            raise HistoryFilterInvalidError(
                {"date_range": ["Start date must be on or before end date."]}
            )
        if period in (HistoryPeriod.CUSTOM, None):
            return start_date, end_date

        today = datetime.now(UTC).date()
        if period in (HistoryPeriod.WEEK, HistoryPeriod.WEEKLY):
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            return start, end
        if period in (HistoryPeriod.MONTH, HistoryPeriod.MONTHLY):
            start = today.replace(day=1)
            # Find last day of month
            if today.month == 12:
                next_month = today.replace(year=today.year + 1, month=1, day=1)
            else:
                next_month = today.replace(month=today.month + 1, day=1)
            end = next_month - timedelta(days=1)
            return start, end
        return start_date, end_date
