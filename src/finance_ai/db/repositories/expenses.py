"""PostgreSQL queries for owned expenses and the unified history feed."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from finance_ai.db.models.accounts import Account
from finance_ai.db.models.catalogs import Category, IncomeSource, Subcategory
from finance_ai.db.models.receipts import Expense, Receipt
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.expenses.repositories import (
    CategoryReference,
    CurrencyReference,
    ExpenseItemRecord,
    ExpenseRecord,
    HistoryExpenseRecord,
    HistoryFilters,
    HistoryMovementRecord,
    HistoryRecord,
)


class SQLAlchemyExpenseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_owned(
        self, *, owner_id: int, offset: int, limit: int
    ) -> tuple[list[ExpenseRecord], int]:
        statement = self._expense_projection(owner_id)
        total = int(
            self.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        )
        rows = self.session.execute(
            statement.order_by(
                FinancialTransaction.occurred_at.desc(),
                FinancialTransaction.id.desc(),
                Expense.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return [self._expense_record(row) for row in rows], total

    def find_owned(
        self, *, owner_id: int, expense_id: int, for_update: bool = False
    ) -> ExpenseRecord | None:
        statement = self._expense_projection(owner_id).where(Expense.id == expense_id)
        if for_update:
            statement = statement.with_for_update(of=Expense)
        row = self.session.execute(statement).first()
        return None if row is None else self._expense_record(row)

    def active_account_currency(
        self, *, owner_id: int, account_id: int
    ) -> CurrencyReference | None:
        row = self.session.execute(
            select(Currency)
            .join(Account, Account.currency_id == Currency.id)
            .where(
                Account.id == account_id,
                Account.user_id == owner_id,
                Account.is_active.is_(True),
            )
        ).scalar_one_or_none()
        return None if row is None else self._currency(row)

    def category_owned(self, *, owner_id: int, category_id: int) -> bool:
        return (
            self.session.scalar(
                select(Category.id).where(Category.id == category_id, Category.user_id == owner_id)
            )
            is not None
        )

    def update(
        self,
        *,
        owner_id: int,
        expense_id: int,
        fields: frozenset[str],
        name: str | None,
        amount: Decimal | None,
        note: str | None,
        receipt_name: str | None,
        receipt_note: str | None,
        category_id: int | None,
        account_id: int | None,
        occurred_at: datetime | None,
        description: str | None,
    ) -> None:
        row = self.session.execute(
            select(Expense.receipt_id, Receipt.transaction_id)
            .join(Receipt, Receipt.id == Expense.receipt_id)
            .join(FinancialTransaction, FinancialTransaction.id == Receipt.transaction_id)
            .where(
                Expense.id == expense_id,
                Expense.user_id == owner_id,
                Receipt.user_id == owner_id,
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.transaction_type == "expense",
            )
            .with_for_update()
        ).first()
        if row is None or row.transaction_id is None:
            return
        receipt_id, transaction_id = row

        expense_values: dict[str, object] = {"updated_at": func.now()}
        if "name" in fields:
            expense_values["name"] = name
        if "amount" in fields:
            expense_values["amount"] = amount
        if "note" in fields:
            expense_values["note"] = note
            expense_values["item_reason"] = note
        if "category_id" in fields:
            expense_values["category_id"] = category_id
            expense_values["subcategory_id"] = None
            # A HU-008 item normally represents the receipt's only line. Keep
            # the receipt-level category aligned with that correction.
            receipt_values_category = category_id
        else:
            receipt_values_category = None
        self.session.execute(update(Expense).where(Expense.id == expense_id).values(expense_values))

        transaction_values: dict[str, object] = {"updated_at": func.now()}
        if "occurred_at" in fields:
            transaction_values["occurred_at"] = occurred_at
        if "description" in fields:
            transaction_values["description"] = description
        self.session.execute(
            update(FinancialTransaction)
            .where(
                FinancialTransaction.id == transaction_id,
                FinancialTransaction.user_id == owner_id,
            )
            .values(transaction_values)
        )

        receipt_values: dict[str, object] = {"updated_at": func.now()}
        if "category_id" in fields:
            receipt_values["category_id"] = receipt_values_category
        if "receipt_name" in fields:
            receipt_values["store"] = receipt_name
        if "receipt_note" in fields:
            receipt_values["purchase_reason"] = receipt_note
        if "occurred_at" in fields and occurred_at is not None:
            receipt_values["date"] = occurred_at.date()
        self.session.flush()
        total = self.session.scalar(
            select(func.sum(Expense.amount)).where(Expense.receipt_id == receipt_id)
        )
        if total is None:
            return
        receipt_values["total"] = total
        self.session.execute(update(Receipt).where(Receipt.id == receipt_id).values(receipt_values))

        movement_values: dict[str, object] = {}
        if account_id is not None:
            movement_values["account_id"] = account_id
        if "amount" in fields:
            movement_values["amount"] = -Decimal(total)
        if movement_values:
            self.session.execute(
                update(AccountMovement)
                .where(
                    AccountMovement.transaction_id == transaction_id,
                    AccountMovement.user_id == owner_id,
                )
                .values(movement_values)
            )

    def delete(self, *, owner_id: int, expense_id: int) -> None:
        transaction_id = self.session.scalar(
            select(Receipt.transaction_id)
            .join(Expense, Expense.receipt_id == Receipt.id)
            .join(FinancialTransaction, FinancialTransaction.id == Receipt.transaction_id)
            .where(
                Expense.id == expense_id,
                Expense.user_id == owner_id,
                Receipt.user_id == owner_id,
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.transaction_type == "expense",
            )
            .with_for_update()
        )
        if transaction_id is not None:
            # Deleting the parent transaction cascades to its receipt, items, and movements.
            # The UoW commits once, so a database error rolls back the complete deletion.
            self.session.execute(
                delete(FinancialTransaction).where(
                    FinancialTransaction.id == transaction_id,
                    FinancialTransaction.user_id == owner_id,
                )
            )

    def list_history(
        self, *, owner_id: int, filters: HistoryFilters
    ) -> tuple[list[HistoryRecord], int]:
        base = self._history_ids(owner_id=owner_id, filters=filters)
        total = int(self.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
        ids = list(
            self.session.scalars(
                base.order_by(
                    FinancialTransaction.occurred_at.desc(),
                    FinancialTransaction.id.desc(),
                )
                .offset(filters.offset)
                .limit(filters.limit)
            )
        )
        if not ids:
            return [], total

        transaction_rows = list(
            self.session.execute(
                select(FinancialTransaction, IncomeSource)
                .outerjoin(
                    IncomeSource,
                    and_(
                        IncomeSource.id == FinancialTransaction.income_source_id,
                        IncomeSource.user_id == owner_id,
                    ),
                )
                .where(
                    FinancialTransaction.id.in_(ids),
                    FinancialTransaction.user_id == owner_id,
                )
            ).all()
        )
        transaction_rows.sort(key=lambda row: (-row[0].occurred_at.timestamp(), -row[0].id))

        movement_rows = self.session.execute(
            select(AccountMovement, Account, Currency)
            .join(
                Account,
                and_(
                    Account.id == AccountMovement.account_id,
                    Account.user_id == AccountMovement.user_id,
                ),
            )
            .join(Currency, Currency.id == Account.currency_id)
            .where(AccountMovement.user_id == owner_id, AccountMovement.transaction_id.in_(ids))
            .order_by(AccountMovement.transaction_id, AccountMovement.id)
        ).all()
        movements: dict[int, list[HistoryMovementRecord]] = defaultdict(list)
        for movement, account, currency in movement_rows:
            movements[movement.transaction_id].append(
                HistoryMovementRecord(
                    account_id=account.id,
                    account_name=account.name,
                    amount=movement.amount,
                    currency=self._currency(currency),
                )
            )

        parent_category = aliased(Category)
        expense_rows = self.session.execute(
            select(Expense, Receipt, Category, parent_category)
            .join(Receipt, Receipt.id == Expense.receipt_id)
            .outerjoin(Category, Category.id == Expense.category_id)
            .outerjoin(Subcategory, Subcategory.id == Expense.subcategory_id)
            .outerjoin(parent_category, parent_category.id == Subcategory.category_id)
            .where(
                Receipt.transaction_id.in_(ids),
                Receipt.user_id == owner_id,
                Expense.user_id == owner_id,
            )
            .order_by(Receipt.transaction_id, Expense.position, Expense.id)
        ).all()
        expense_groups: dict[
            int, list[tuple[Expense, Receipt, Category | None, Category | None]]
        ] = defaultdict(list)
        for expense, receipt, category, legacy_category in expense_rows:
            expense_groups[receipt.transaction_id].append(
                (expense, receipt, category, legacy_category)
            )

        result: list[HistoryRecord] = []
        for transaction, income_source in transaction_rows:
            group = expense_groups.get(transaction.id, [])
            result.append(
                HistoryRecord(
                    id=transaction.id,
                    transaction_type=transaction.transaction_type,
                    status=transaction.status,
                    occurred_at=transaction.occurred_at,
                    description=transaction.description,
                    capture_source=transaction.capture_source,
                    income_source_name=None if income_source is None else income_source.name,
                    movements=tuple(movements.get(transaction.id, [])),
                    expense=self._history_expense(group) if group else None,
                )
            )
        return result, total

    def _history_ids(self, *, owner_id: int, filters: HistoryFilters) -> Select[tuple[int]]:
        statement = select(FinancialTransaction.id).where(
            FinancialTransaction.user_id == owner_id,
            FinancialTransaction.transaction_type.in_(("expense", "income", "transfer")),
        )
        if filters.transaction_type is not None:
            statement = statement.where(
                FinancialTransaction.transaction_type == filters.transaction_type
            )
        if filters.start_date is not None:
            statement = statement.where(
                FinancialTransaction.occurred_at >= self._start_of_day(filters.start_date)
            )
        if filters.end_date is not None:
            statement = statement.where(
                FinancialTransaction.occurred_at
                < self._start_of_day(filters.end_date) + timedelta(days=1)
            )
        if filters.account_id is not None:
            statement = statement.where(
                exists(
                    select(AccountMovement.id).where(
                        AccountMovement.transaction_id == FinancialTransaction.id,
                        AccountMovement.user_id == owner_id,
                        AccountMovement.account_id == filters.account_id,
                    )
                )
            )
        if filters.category_id is not None:
            statement = statement.where(
                exists(
                    select(Expense.id)
                    .join(Receipt, Receipt.id == Expense.receipt_id)
                    .outerjoin(Subcategory, Subcategory.id == Expense.subcategory_id)
                    .where(
                        Receipt.transaction_id == FinancialTransaction.id,
                        Expense.user_id == owner_id,
                        Receipt.user_id == owner_id,
                        or_(
                            Expense.category_id == filters.category_id,
                            Receipt.category_id == filters.category_id,
                            Subcategory.category_id == filters.category_id,
                        ),
                    )
                )
            )
        if filters.search is not None:
            pattern = self._search_pattern(filters.search)
            statement = statement.where(
                or_(
                    func.lower(func.coalesce(FinancialTransaction.description, "")).like(
                        pattern, escape="\\"
                    ),
                    exists(
                        select(IncomeSource.id).where(
                            IncomeSource.id == FinancialTransaction.income_source_id,
                            IncomeSource.user_id == owner_id,
                            func.lower(IncomeSource.name).like(pattern, escape="\\"),
                        )
                    ),
                    exists(
                        select(Expense.id)
                        .join(Receipt, Receipt.id == Expense.receipt_id)
                        .where(
                            Receipt.transaction_id == FinancialTransaction.id,
                            Expense.user_id == owner_id,
                            Receipt.user_id == owner_id,
                            or_(
                                func.lower(Receipt.store).like(pattern, escape="\\"),
                                func.lower(func.coalesce(Receipt.purchase_reason, "")).like(
                                    pattern, escape="\\"
                                ),
                                func.lower(Expense.name).like(pattern, escape="\\"),
                                func.lower(func.coalesce(Expense.item_reason, "")).like(
                                    pattern, escape="\\"
                                ),
                                func.lower(func.coalesce(Expense.note, "")).like(
                                    pattern, escape="\\"
                                ),
                            ),
                        )
                    ),
                )
            )
        return statement

    def _expense_projection(self, owner_id: int) -> Select[Any]:
        parent_category = aliased(Category)
        return (
            select(
                Expense,
                Receipt,
                FinancialTransaction,
                AccountMovement,
                Account,
                Currency,
                Category,
                parent_category,
            )
            .join(Receipt, Receipt.id == Expense.receipt_id)
            .join(FinancialTransaction, FinancialTransaction.id == Receipt.transaction_id)
            .join(
                AccountMovement,
                and_(
                    AccountMovement.transaction_id == FinancialTransaction.id,
                    AccountMovement.user_id == owner_id,
                ),
            )
            .join(
                Account,
                and_(Account.id == AccountMovement.account_id, Account.user_id == owner_id),
            )
            .join(Currency, Currency.id == Account.currency_id)
            .outerjoin(Category, Category.id == Expense.category_id)
            .outerjoin(Subcategory, Subcategory.id == Expense.subcategory_id)
            .outerjoin(parent_category, parent_category.id == Subcategory.category_id)
            .where(
                Expense.user_id == owner_id,
                Receipt.user_id == owner_id,
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.transaction_type == "expense",
            )
        )

    @staticmethod
    def _expense_record(row: Any) -> ExpenseRecord:
        expense, receipt, transaction, _movement, account, currency, category, legacy_category = row
        selected_category = category or legacy_category
        return ExpenseRecord(
            id=expense.id,
            transaction_id=transaction.id,
            receipt_id=receipt.id,
            name=expense.name,
            amount=expense.amount,
            quantity=expense.quantity,
            unit_price=expense.unit_price,
            note=expense.note or expense.item_reason,
            category=None
            if selected_category is None
            else CategoryReference(selected_category.id, selected_category.name),
            receipt_name=receipt.store,
            receipt_note=receipt.purchase_reason,
            store=receipt.store,
            description=transaction.description,
            occurred_at=transaction.occurred_at,
            status=transaction.status,
            capture_source=transaction.capture_source,
            account_id=account.id,
            account_name=account.name,
            account_is_active=account.is_active,
            currency=SQLAlchemyExpenseRepository._currency(currency),
            created_at=expense.created_at,
            updated_at=expense.updated_at,
            receipt_image=receipt.image,
            image_path=expense.image_path,
        )

    @staticmethod
    def _history_expense(
        group: list[tuple[Expense, Receipt, Category | None, Category | None]],
    ) -> HistoryExpenseRecord:
        _first_expense, receipt, _, _ = group[0]
        return HistoryExpenseRecord(
            receipt_id=receipt.id,
            name=receipt.store,
            occurred_at=datetime.combine(receipt.date, datetime.min.time(), tzinfo=UTC),
            total_amount=receipt.total,
            note=receipt.purchase_reason,
            store=receipt.store,
            receipt_image=receipt.image,
            items=tuple(
                ExpenseItemRecord(
                    id=expense.id,
                    name=expense.name,
                    amount=expense.amount,
                    quantity=expense.quantity,
                    unit_price=expense.unit_price,
                    note=expense.note or expense.item_reason,
                    category=SQLAlchemyExpenseRepository._category_reference(
                        category, legacy_category
                    ),
                    image_path=expense.image_path,
                )
                for expense, _, category, legacy_category in group
            ),
        )

    @staticmethod
    def _currency(currency: Currency) -> CurrencyReference:
        return CurrencyReference(
            id=currency.id,
            code=currency.code,
            name=currency.name,
            decimals=currency.decimals,
        )

    @staticmethod
    def _category_reference(
        category: Category | None, legacy_category: Category | None
    ) -> CategoryReference | None:
        selected = category if category is not None else legacy_category
        return None if selected is None else CategoryReference(selected.id, selected.name)

    @staticmethod
    def _start_of_day(value: date) -> datetime:
        return datetime(value.year, value.month, value.day, tzinfo=UTC)

    @staticmethod
    def _search_pattern(value: str) -> str:
        escaped = value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"
