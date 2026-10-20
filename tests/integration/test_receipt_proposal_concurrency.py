"""PostgreSQL row-lock regressions for receipt proposal retries and confirmation."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.core.config import get_settings
from finance_ai.db.expense_uow import SQLAlchemyExpenseUnitOfWork
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.receipts import Expense, Receipt
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.expenses.service import ExpenseService
from finance_ai.receipts.models import ReceiptProposalModel
from finance_ai.receipts.repository import ReceiptProposalRepository
from finance_ai.receipts.schemas import ReceiptProposalConfirmRequest, ReceiptProposalResponse

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
LOCK_TIMEOUT = "5s"
WAIT_TIMEOUT_SECONDS = 8


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_users(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE TABLE app_user RESTART IDENTITY CASCADE"))
        session.commit()


def create_owner() -> int:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        owner = AppUser(
            email=f"receipt-concurrency-{uuid4()}@example.test",
            password_hash="$argon2id$synthetic",
            preferred_currency_id=currency_id,
            is_active=True,
        )
        session.add(owner)
        session.commit()
        return owner.id


def create_proposal(owner_id: int, *, status: str = "complete") -> ReceiptProposalResponse:
    now = datetime.now(UTC)
    proposal = ReceiptProposalResponse(
        proposal_id=uuid4(),
        status=status,
        revision=1,
        merchant_name="Concurrency Test Shop",
        total_amount="1250.00",
        currency="COP",
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    with get_session_factory()() as session:
        ReceiptProposalRepository(session).create(owner_id, proposal)
        session.commit()
    return proposal


def set_lock_timeout(session: Session) -> None:
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, false)"), {"timeout": LOCK_TIMEOUT}
    )


def wait_for_lock_wait(session_factory: sessionmaker[Session], proposal_id: UUID) -> None:
    """Wait until PostgreSQL confirms another backend is blocked on this proposal row."""
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with session_factory() as observer:
            blocked = observer.scalar(
                text(
                    "SELECT EXISTS ("
                    " SELECT 1 FROM pg_stat_activity "
                    " WHERE wait_event_type = 'Lock' AND query ILIKE '%receipt_proposals%'"
                    ")"
                )
            )
            if blocked:
                return
        time.sleep(0.025)
    pytest.fail(f"Timed out waiting for PostgreSQL row lock on proposal {proposal_id}")


def locked_worker(
    session_factory: sessionmaker[Session],
    started: Event,
    operation: str,
    owner_id: int,
    proposal_id: UUID,
) -> bool:
    with session_factory() as session:
        set_lock_timeout(session)
        started.set()
        repository = ReceiptProposalRepository(session)
        if operation == "confirm":
            result = repository.mark_confirmed(owner_id, proposal_id, transaction_id=7001)
        else:
            stale = ReceiptProposalResponse(
                proposal_id=proposal_id,
                status="complete",
                revision=2,
                merchant_name="Stale retry payload",
                total_amount="9999.00",
                currency="COP",
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            result = repository.update(owner_id, proposal_id, stale) is not None
        session.commit()
        return result


def row_state(proposal_id: UUID) -> tuple[str, dict[str, object], int]:
    with get_session_factory()() as session:
        row = session.get(ReceiptProposalModel, proposal_id)
        assert row is not None
        return row.status, dict(row.proposal_data), row.revision


def test_retry_waiting_behind_confirmation_cannot_reopen_or_change_transaction() -> None:
    owner_id = create_owner()
    proposal = create_proposal(owner_id)
    session_factory = get_session_factory()
    started = Event()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with session_factory() as confirmer:
            set_lock_timeout(confirmer)
            row = ReceiptProposalRepository(confirmer).get_by_id_for_user(
                owner_id, proposal.proposal_id, for_update=True
            )
            assert row is not None
            row.status = "confirmed"
            data = dict(row.proposal_data)
            data["transaction_id"] = 7001
            row.proposal_data = data
            pending = executor.submit(
                locked_worker, session_factory, started, "retry", owner_id, proposal.proposal_id
            )
            assert started.wait(WAIT_TIMEOUT_SECONDS)
            wait_for_lock_wait(session_factory, proposal.proposal_id)
            confirmer.commit()
        assert pending.result(timeout=WAIT_TIMEOUT_SECONDS) is True

    status, data, revision = row_state(proposal.proposal_id)
    assert status == "confirmed"
    assert data["transaction_id"] == 7001
    assert data["merchant_name"] == "Concurrency Test Shop"
    assert revision == 1


def test_confirmation_waiting_behind_retry_commits_terminal_state_once() -> None:
    owner_id = create_owner()
    proposal = create_proposal(owner_id)
    session_factory = get_session_factory()
    started = Event()
    stale = ReceiptProposalResponse(
        proposal_id=proposal.proposal_id,
        status="complete",
        revision=2,
        merchant_name="Retried payload",
        total_amount="1250.00",
        currency="COP",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with session_factory() as retrying:
            set_lock_timeout(retrying)
            updated = ReceiptProposalRepository(retrying).update(
                owner_id, proposal.proposal_id, stale
            )
            assert updated is not None
            pending = executor.submit(
                locked_worker, session_factory, started, "confirm", owner_id, proposal.proposal_id
            )
            assert started.wait(WAIT_TIMEOUT_SECONDS)
            wait_for_lock_wait(session_factory, proposal.proposal_id)
            retrying.commit()
        assert pending.result(timeout=WAIT_TIMEOUT_SECONDS) is True

    status, data, revision = row_state(proposal.proposal_id)
    assert status == "confirmed"
    assert data["transaction_id"] == 7001
    assert data["merchant_name"] == "Retried payload"
    assert revision == 2


@pytest.mark.parametrize("terminal_status", ["confirmed", "discarded"])
def test_terminal_state_rejects_late_retry_and_preserves_transaction_id(
    terminal_status: str,
) -> None:
    owner_id = create_owner()
    proposal = create_proposal(owner_id)
    with get_session_factory()() as session:
        row = ReceiptProposalRepository(session).get_by_id_for_user(owner_id, proposal.proposal_id)
        assert row is not None
        row.status = terminal_status
        data = dict(row.proposal_data)
        data["transaction_id"] = 7001
        row.proposal_data = data
        session.commit()

    stale = proposal.model_copy(update={"revision": 9, "merchant_name": "Late retry"})
    with get_session_factory()() as session:
        updated = ReceiptProposalRepository(session).update(owner_id, proposal.proposal_id, stale)
        assert updated is not None
        session.commit()
    assert row_state(proposal.proposal_id) == (terminal_status, data, 1)


def test_rollback_of_confirmation_releases_lock_and_retry_can_recover() -> None:
    owner_id = create_owner()
    proposal = create_proposal(owner_id)
    session_factory = get_session_factory()
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with session_factory() as confirmer:
            set_lock_timeout(confirmer)
            row = ReceiptProposalRepository(confirmer).get_by_id_for_user(
                owner_id, proposal.proposal_id, for_update=True
            )
            assert row is not None
            row.status = "confirmed"
            pending = executor.submit(
                locked_worker, session_factory, started, "retry", owner_id, proposal.proposal_id
            )
            assert started.wait(WAIT_TIMEOUT_SECONDS)
            wait_for_lock_wait(session_factory, proposal.proposal_id)
            confirmer.rollback()
        assert pending.result(timeout=WAIT_TIMEOUT_SECONDS) is True

    status, data, revision = row_state(proposal.proposal_id)
    assert status == "complete"
    assert "transaction_id" not in data
    assert data["merchant_name"] == "Stale retry payload"
    assert revision == 2


def test_two_concurrent_real_confirmations_create_one_expense() -> None:
    owner_id = create_owner()
    proposal = create_proposal(owner_id)
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        account = Account(
            user_id=owner_id,
            name="Concurrent receipt cash",
            account_type="cash",
            currency_id=currency_id,
            opening_balance="50000.00",
            is_active=True,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    command = ReceiptProposalConfirmRequest(
        account_id=account_id,
        total_amount="1250.00",
        merchant="Concurrency Test Shop",
        occurred_at=datetime.now(UTC),
        currency="COP",
    )
    barrier = Barrier(2)

    def confirm() -> tuple[int, bool]:
        barrier.wait(timeout=WAIT_TIMEOUT_SECONDS)
        service = ExpenseService(
            uow_factory=lambda: SQLAlchemyExpenseUnitOfWork(get_session_factory())
        )
        record, is_new = service.confirm_receipt_proposal(
            owner_id=owner_id, proposal_id=proposal.proposal_id, command=command
        )
        return record.transaction_id, is_new

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(confirm)
        second = executor.submit(confirm)
        results = [
            first.result(timeout=WAIT_TIMEOUT_SECONDS),
            second.result(timeout=WAIT_TIMEOUT_SECONDS),
        ]

    transaction_ids = {transaction_id for transaction_id, _ in results}
    assert len(transaction_ids) == 1
    assert sorted(is_new for _, is_new in results) == [False, True]
    transaction_id = next(iter(transaction_ids))
    with get_session_factory()() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(FinancialTransaction)
                .where(
                    FinancialTransaction.id == transaction_id,
                    FinancialTransaction.user_id == owner_id,
                    FinancialTransaction.capture_source == "receipt_photo_ai",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Receipt)
                .where(Receipt.transaction_id == transaction_id, Receipt.user_id == owner_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Expense)
                .join(Receipt, Expense.receipt_id == Receipt.id)
                .where(Receipt.transaction_id == transaction_id, Expense.user_id == owner_id)
            )
            == 1
        )
    status, data, _ = row_state(proposal.proposal_id)
    assert status == "confirmed"
    assert data["transaction_id"] == transaction_id
