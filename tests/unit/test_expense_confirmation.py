"""Unit tests for HU-015 Unit 2 Expense Confirmation business rules & service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from finance_ai.core.errors import (
    ExpenseAccountInvalidError,
    ExpenseCategoryNotFoundError,
    ExpenseNotFoundError,
    RequestInvalidError,
)
from finance_ai.expenses.repositories import (
    CategoryReference,
    CurrencyReference,
    ExpenseRecord,
)
from finance_ai.expenses.service import ExpenseService
from finance_ai.receipts.models import ReceiptProposalModel
from finance_ai.receipts.schemas import (
    ConfirmedReceiptItem,
    ReceiptProposalConfirmRequest,
)

NOW = datetime.now(UTC)
COP = CurrencyReference(id=1, code="COP", name="Colombian Peso", decimals=2)
USD = CurrencyReference(id=2, code="USD", name="US Dollar", decimals=2)


def make_expense_record(
    *,
    expense_id: int = 10,
    transaction_id: int = 20,
    receipt_id: int = 30,
    amount: Decimal = Decimal("45000.00"),
    currency: CurrencyReference = COP,
    merchant: str = "Éxito Calle 80",
) -> ExpenseRecord:
    return ExpenseRecord(
        id=expense_id,
        transaction_id=transaction_id,
        receipt_id=receipt_id,
        name=merchant,
        amount=amount,
        quantity=Decimal("1"),
        unit_price=amount,
        note="Weekly groceries",
        category=CategoryReference(5, "Groceries"),
        receipt_name=merchant,
        receipt_note="Weekly groceries",
        store=merchant,
        description=merchant,
        occurred_at=NOW,
        status="confirmed",
        capture_source="receipt_photo_ai",
        account_id=1,
        account_name="Main Checking",
        account_is_active=True,
        currency=currency,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeProposalRepository:
    def __init__(self, proposals: dict[UUID, ReceiptProposalModel]) -> None:
        self.proposals = proposals
        self.locked_calls: list[tuple[int, UUID, bool]] = []
        self.confirmed_calls: list[tuple[int, UUID, int | None]] = []

    def get_by_id_for_user(
        self, user_id: int, proposal_id: UUID, *, for_update: bool = False
    ) -> ReceiptProposalModel | None:
        self.locked_calls.append((user_id, proposal_id, for_update))
        proposal = self.proposals.get(proposal_id)
        if proposal and proposal.user_id == user_id:
            return proposal
        return None

    def mark_confirmed(
        self, user_id: int, proposal_id: UUID, *, transaction_id: int | None = None
    ) -> bool:
        self.confirmed_calls.append((user_id, proposal_id, transaction_id))
        proposal = self.proposals.get(proposal_id)
        if proposal and proposal.user_id == user_id:
            proposal.status = "confirmed"
            if transaction_id is not None:
                proposal.proposal_data = dict(proposal.proposal_data)
                proposal.proposal_data["transaction_id"] = transaction_id
            return True
        return False


class FakeExpenseRepository:
    def __init__(self) -> None:
        self.active_accounts: dict[int, CurrencyReference] = {1: COP, 2: USD}
        self.owned_categories: set[int] = {5, 8}
        self.created_calls: list[dict[str, Any]] = []
        self.transactions: dict[int, ExpenseRecord] = {}

    def active_account_currency(
        self, *, owner_id: int, account_id: int
    ) -> CurrencyReference | None:
        if owner_id == 100:
            return self.active_accounts.get(account_id)
        return None

    def category_owned(self, *, owner_id: int, category_id: int) -> bool:
        return owner_id == 100 and category_id in self.owned_categories

    def create_from_receipt_proposal(
        self,
        *,
        owner_id: int,
        account_id: int,
        total_amount: Decimal,
        merchant: str,
        occurred_at: datetime,
        category_id: int | None = None,
        items: list[ConfirmedReceiptItem] | None = None,
        note: str | None = None,
    ) -> ExpenseRecord:
        record = make_expense_record(
            amount=total_amount,
            merchant=merchant,
            currency=self.active_accounts[account_id],
        )
        self.created_calls.append(
            {
                "owner_id": owner_id,
                "account_id": account_id,
                "total_amount": total_amount,
                "merchant": merchant,
                "occurred_at": occurred_at,
                "category_id": category_id,
                "items": items,
                "note": note,
            }
        )
        self.transactions[record.transaction_id] = record
        return record

    def find_by_transaction_id(self, *, owner_id: int, transaction_id: int) -> ExpenseRecord | None:
        if owner_id == 100:
            return self.transactions.get(transaction_id)
        return None

    def list_owned(self, **kwargs: Any) -> Any: ...
    def find_owned(self, **kwargs: Any) -> Any: ...
    def create_manual(self, **kwargs: Any) -> Any: ...
    def update(self, **kwargs: Any) -> Any: ...
    def delete(self, **kwargs: Any) -> Any: ...
    def list_history(self, **kwargs: Any) -> Any: ...


class FakeCategoryRepository:
    def __init__(self, fake_expenses: FakeExpenseRepository | None = None) -> None:
        self.categories: dict[int, str] = {5: "Groceries", 8: "Utilities"}
        self.fake_expenses = fake_expenses
        self.next_id = 99

    def list_owned(self, *, owner_id: int) -> list[Any]:
        class Cat:
            def __init__(self, id: int, name: str) -> None:
                self.id = id
                self.name = name

        return [Cat(k, v) for k, v in self.categories.items()]

    def add(self, *, owner_id: int, name: str, **kwargs: Any) -> int:
        cat_id = self.next_id
        self.next_id += 1
        self.categories[cat_id] = name
        if self.fake_expenses is not None:
            self.fake_expenses.owned_categories.add(cat_id)
        return cat_id


class FakeUnitOfWork:
    def __init__(
        self,
        proposals: FakeProposalRepository,
        expenses: FakeExpenseRepository,
        categories: FakeCategoryRepository | None = None,
    ) -> None:
        self.proposals = proposals
        self.expenses = expenses
        self.categories = categories or FakeCategoryRepository(expenses)
        self.committed = False
        self.rolled_back = False
        self.flushed = False

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None or not self.committed:
            self.rolled_back = True

    def flush(self) -> None:
        self.flushed = True

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def service_setup() -> tuple[
    ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository
]:
    proposals_map: dict[UUID, ReceiptProposalModel] = {}
    fake_proposals = FakeProposalRepository(proposals_map)
    fake_expenses = FakeExpenseRepository()

    def uow_factory():
        return FakeUnitOfWork(fake_proposals, fake_expenses)

    service = ExpenseService(uow_factory=uow_factory)
    return service, proposals_map, fake_expenses


def test_confirm_proposal_success_with_items(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, fake_expenses = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={"merchant_name": "Éxito", "total_amount": 45000.0},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("45000.00"),
        merchant="Éxito Calle 80",
        occurred_at=NOW,
        currency="COP",
        category_id=5,
        items=[
            ConfirmedReceiptItem(
                name="Leche 1L",
                total_price=Decimal("10000.00"),
                quantity=Decimal("2"),
                unit_price=Decimal("5000.00"),
                category_id=5,
            ),
            ConfirmedReceiptItem(
                name="Carne Res 1kg",
                total_price=Decimal("35000.00"),
                quantity=Decimal("1"),
                unit_price=Decimal("35000.00"),
            ),
        ],
    )

    record, is_new = service.confirm_receipt_proposal(
        owner_id=100, proposal_id=proposal_id, command=cmd
    )

    assert is_new is True
    assert record.amount == Decimal("45000.00")
    assert record.capture_source == "receipt_photo_ai"
    assert len(fake_expenses.created_calls) == 1
    assert len(fake_expenses.created_calls[0]["items"]) == 2
    assert proposals_map[proposal_id].status == "confirmed"


def test_confirm_proposal_fallback_empty_items(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, fake_expenses = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="partial",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("15000.00"),
        merchant="Tienda D1",
        occurred_at=NOW,
        currency="COP",
        items=[],
    )

    record, is_new = service.confirm_receipt_proposal(
        owner_id=100, proposal_id=proposal_id, command=cmd
    )

    assert is_new is True
    assert record.amount == Decimal("15000.00")
    assert fake_expenses.created_calls[0]["items"] == []


def test_confirm_proposal_idempotency_replay(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, fake_expenses = service_setup
    proposal_id = uuid4()
    existing_record = make_expense_record(transaction_id=999)
    fake_expenses.transactions[999] = existing_record

    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="confirmed",
        proposal_data={"transaction_id": 999},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("45000.00"),
        merchant="Éxito",
        occurred_at=NOW,
        currency="COP",
    )

    record, is_new = service.confirm_receipt_proposal(
        owner_id=100, proposal_id=proposal_id, command=cmd
    )

    assert is_new is False
    assert record.id == existing_record.id
    # No new creation call made
    assert len(fake_expenses.created_calls) == 0


def test_confirm_proposal_not_found(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, _, _ = service_setup
    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("10000.00"),
        merchant="Test",
        occurred_at=NOW,
        currency="COP",
    )
    with pytest.raises(ExpenseNotFoundError):
        service.confirm_receipt_proposal(owner_id=100, proposal_id=uuid4(), command=cmd)


def test_confirm_proposal_expired_rejected(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, _ = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={},
        revision=1,
        created_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),  # Expired
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("10000.00"),
        merchant="Test",
        occurred_at=NOW,
        currency="COP",
    )
    with pytest.raises(RequestInvalidError) as exc_info:
        service.confirm_receipt_proposal(owner_id=100, proposal_id=proposal_id, command=cmd)
    assert exc_info.value.field_errors is not None
    assert "expired" in str(exc_info.value.field_errors["proposal"][0])


def test_confirm_proposal_discarded_rejected(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, _ = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="discarded",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("10000.00"),
        merchant="Test",
        occurred_at=NOW,
        currency="COP",
    )
    with pytest.raises(RequestInvalidError) as exc_info:
        service.confirm_receipt_proposal(owner_id=100, proposal_id=proposal_id, command=cmd)
    assert exc_info.value.field_errors is not None
    assert "discarded" in str(exc_info.value.field_errors["proposal"][0])


def test_confirm_proposal_not_a_receipt_rejected(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, _ = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="not_a_receipt",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("10000.00"),
        merchant="Test",
        occurred_at=NOW,
        currency="COP",
    )
    with pytest.raises(RequestInvalidError) as exc_info:
        service.confirm_receipt_proposal(owner_id=100, proposal_id=proposal_id, command=cmd)
    assert exc_info.value.field_errors is not None
    assert "not a receipt" in str(exc_info.value.field_errors["proposal"][0])


def test_confirm_proposal_currency_mismatch_rejected(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, _ = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    # Account 1 is COP, but payload specifies USD
    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("100.00"),
        merchant="Amazon US",
        occurred_at=NOW,
        currency="USD",
    )
    with pytest.raises(ExpenseAccountInvalidError):
        service.confirm_receipt_proposal(owner_id=100, proposal_id=proposal_id, command=cmd)


def test_confirm_proposal_unowned_category_rejected(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, _ = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("10000.00"),
        merchant="Test",
        occurred_at=NOW,
        currency="COP",
        category_id=999,  # Unowned category
    )
    with pytest.raises(ExpenseCategoryNotFoundError):
        service.confirm_receipt_proposal(owner_id=100, proposal_id=proposal_id, command=cmd)


def test_confirm_proposal_unowned_item_category_rejected(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, _ = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("10000.00"),
        merchant="Test",
        occurred_at=NOW,
        currency="COP",
        category_id=5,
        items=[
            ConfirmedReceiptItem(
                name="Item with invalid category",
                total_price=Decimal("10000.00"),
                category_id=999,  # Unowned
            )
        ],
    )
    with pytest.raises(ExpenseCategoryNotFoundError):
        service.confirm_receipt_proposal(owner_id=100, proposal_id=proposal_id, command=cmd)


def test_confirm_proposal_with_new_category_name_creates_category(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, fake_expenses = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("15000.00"),
        merchant="Pet Store",
        occurred_at=NOW,
        currency="COP",
        category_id=None,
        new_category_name="Mascotas",
    )
    record, is_new = service.confirm_receipt_proposal(
        owner_id=100, proposal_id=proposal_id, command=cmd
    )
    assert is_new is True
    assert record is not None
    created_call = fake_expenses.created_calls[-1]
    assert created_call["category_id"] == 99  # New category assigned


def test_confirm_proposal_with_existing_category_name_reuses_category(
    service_setup: tuple[ExpenseService, dict[UUID, ReceiptProposalModel], FakeExpenseRepository],
) -> None:
    service, proposals_map, fake_expenses = service_setup
    proposal_id = uuid4()
    proposals_map[proposal_id] = ReceiptProposalModel(
        id=proposal_id,
        user_id=100,
        status="complete",
        proposal_data={},
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cmd = ReceiptProposalConfirmRequest(
        account_id=1,
        total_amount=Decimal("25000.00"),
        merchant="Supermarket",
        occurred_at=NOW,
        currency="COP",
        category_id=None,
        new_category_name="Groceries",  # Already exists as ID 5
    )
    record, is_new = service.confirm_receipt_proposal(
        owner_id=100, proposal_id=proposal_id, command=cmd
    )
    assert is_new is True
    assert record is not None
    created_call = fake_expenses.created_calls[-1]
    assert created_call["category_id"] == 5  # Reused existing ID 5
