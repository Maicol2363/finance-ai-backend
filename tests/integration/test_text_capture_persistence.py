"""HU-013 on real PostgreSQL: the confirmed operation is the manual one, marked `text_ai`."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.schemas import AccountCreateCommand, AccountType
from finance_ai.accounts.service import AccountService
from finance_ai.capture.dependencies import load_capture_context
from finance_ai.capture.models import TextCaptureProposalModel
from finance_ai.capture.schemas import (
    CaptureIntent,
    ProposalStatus,
    TextProposalConfirmRequest,
    TextProposalRequest,
)
from finance_ai.capture.service import TextCaptureService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import ExpenseAccountInvalidError
from finance_ai.db.account_uow import SQLAlchemyAccountUnitOfWork
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.expenses.dependencies import get_expense_service
from finance_ai.incomes.dependencies import get_income_service
from finance_ai.transfers.dependencies import get_transfer_service
from tests.capture_support import CLIENT_NOW, FakeGateway, raw

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


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


def create_owner(email: str) -> int:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        user = AppUser(
            email=email,
            password_hash="$argon2id$synthetic",
            preferred_currency_id=currency_id,
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def account_service() -> AccountService:
    return AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    )


def create_account(owner_id: int, name: str, opening: str = "100000.00") -> int:
    return (
        account_service()
        .create(
            owner_id=owner_id,
            command=AccountCreateCommand(
                name=name,
                account_type=AccountType.CASH,
                currency_code="COP",
                opening_balance=Decimal(opening),
            ),
        )
        .id
    )


def balance(owner_id: int, account_id: int) -> Decimal:
    return account_service().get(owner_id=owner_id, account_id=account_id).balance


def build(*answers: object) -> TextCaptureService:
    return TextCaptureService(
        session_factory=get_session_factory(),
        gateway=FakeGateway(*answers),  # type: ignore[arg-type]
        context_loader=load_capture_context,
        expenses=get_expense_service(),
        incomes=get_income_service(),
        transfers=get_transfer_service(),
    )


def propose(service: TextCaptureService, owner_id: int, text_: str):
    return asyncio.run(
        service.propose(
            owner_id=owner_id,
            request=TextProposalRequest(text=text_, client_now=CLIENT_NOW),
        )
    )


def capture_sources(owner_id: int) -> dict[str, str]:
    with get_session_factory()() as session:
        rows = session.execute(
            select(
                FinancialTransaction.transaction_type, FinancialTransaction.capture_source
            ).where(FinancialTransaction.user_id == owner_id)
        ).all()
    return {kind: source for kind, source in rows}


def test_the_catalog_context_reads_only_the_owners_active_accounts() -> None:
    owner = create_owner("capture-context@example.test")
    other = create_owner("capture-other@example.test")
    cash = create_account(owner, "Efectivo")
    create_account(other, "Ajena")
    inactive = create_account(owner, "Vieja")
    account_service().set_active(owner_id=owner, account_id=inactive, is_active=False)

    context = load_capture_context(owner)

    names = {a.name for a in context.accounts}
    assert names == {"Efectivo"}
    assert context.account(cash) is not None


def test_text_proposals_save_nothing_and_confirming_records_one_text_ai_expense() -> None:
    owner = create_owner("capture-expense@example.test")
    cash = create_account(owner, "Efectivo")
    create_account(owner, "Nequi")
    service = build(
        raw(intent="expense", amount=15900, description="Almuerzo en Subway", account="Efectivo")
    )

    proposal = propose(service, owner, "Almorcé en Subway 15900 con efectivo")

    assert proposal.status is ProposalStatus.READY
    assert capture_sources(owner) == {}
    assert balance(owner, cash) == Decimal("100000.00")
    confirmation = service.confirm(
        owner_id=owner,
        proposal_id=proposal.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.EXPENSE,
            amount="15900.00",  # type: ignore[arg-type]
            date=proposal.date,  # type: ignore[arg-type]
            description="Almuerzo en Subway",
            account_id=cash,
        ),
    )
    repeat = service.confirm(
        owner_id=owner,
        proposal_id=proposal.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.EXPENSE,
            amount="15900.00",  # type: ignore[arg-type]
            date=proposal.date,  # type: ignore[arg-type]
            account_id=cash,
        ),
    )

    assert capture_sources(owner) == {"expense": "text_ai"}
    assert balance(owner, cash) == Decimal("84100.00")
    assert repeat.replayed is True and repeat.record_id == confirmation.record_id


def test_confirming_an_income_and_a_transfer_uses_the_same_atomic_operations() -> None:
    owner = create_owner("capture-flows@example.test")
    cash = create_account(owner, "Efectivo", "0.00")
    nequi = create_account(owner, "Nequi", "50000.00")
    service = build(
        raw(intent="income", amount=200000, account="Efectivo", description="Venta"),
        raw(intent="expense", amount=30000, account="Nequi", destination_account="Efectivo"),
    )

    income = propose(service, owner, "me pagaron 200 mil en efectivo")
    service.confirm(
        owner_id=owner,
        proposal_id=income.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.INCOME,
            amount="200000.00",  # type: ignore[arg-type]
            date=income.date,  # type: ignore[arg-type]
            account_id=cash,
        ),
    )
    transfer = propose(service, owner, "pasé 30 mil de Nequi a efectivo")
    assert transfer.intent is CaptureIntent.TRANSFER
    service.confirm(
        owner_id=owner,
        proposal_id=transfer.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.TRANSFER,
            amount="30000.00",  # type: ignore[arg-type]
            date=transfer.date,  # type: ignore[arg-type]
            account_id=nequi,
            destination_account_id=cash,
        ),
    )

    assert capture_sources(owner) == {"income": "text_ai", "transfer": "text_ai"}
    assert balance(owner, cash) == Decimal("230000.00")
    assert balance(owner, nequi) == Decimal("20000.00")


def test_manual_rules_still_apply_to_a_text_confirmation() -> None:
    owner = create_owner("capture-rules@example.test")
    stranger = create_owner("capture-stranger@example.test")
    foreign_account = create_account(stranger, "Ajena")
    cash = create_account(owner, "Efectivo")
    service = build(raw(intent="expense", amount=1000, account="Efectivo", description="Gasto"))
    proposal = propose(service, owner, "gasto de mil")

    with pytest.raises(ExpenseAccountInvalidError):
        service.confirm(
            owner_id=owner,
            proposal_id=proposal.proposal_id,
            command=TextProposalConfirmRequest(
                intent=CaptureIntent.EXPENSE,
                amount="1000.00",  # type: ignore[arg-type]
                date=proposal.date,  # type: ignore[arg-type]
                account_id=foreign_account,
            ),
        )

    assert capture_sources(owner) == {}
    assert balance(owner, cash) == Decimal("100000.00")
    with get_session_factory()() as session:
        row = session.scalar(select(TextCaptureProposalModel))
        assert row is not None and row.status == "ready"


def test_drafts_disappear_with_their_user() -> None:
    owner = create_owner("capture-cascade@example.test")
    create_account(owner, "Efectivo")
    propose(
        build(raw(intent="expense", amount=1000, account="Efectivo", description="Gasto")),
        owner,
        "mil",
    )

    with get_session_factory()() as session:
        session.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": owner})
        session.commit()
        assert session.scalar(select(TextCaptureProposalModel)) is None
