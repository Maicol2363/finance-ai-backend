"""HU-013: proposing without saving, conversational corrections and the confirmed operation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.capture.gateway import TextProviderError
from finance_ai.capture.models import TextCaptureProposalModel
from finance_ai.capture.schemas import (
    CaptureIntent,
    ProposalStatus,
    TextChoice,
    TextProposalConfirmRequest,
    TextProposalRequest,
)
from finance_ai.core.errors import (
    RequestInvalidError,
    TextCaptureNotConfiguredError,
    TextCaptureUnavailableError,
    TextProposalClosedError,
    TextProposalNotFoundError,
)
from tests.capture_support import (
    CLIENT_NOW,
    FakeGateway,
    build_service,
    raw,
)

OWNER = 101
EXPENSE_TEXT = "Almorcé en Subway 15900 con efectivo"


def turn(text: str = EXPENSE_TEXT, **extra: object) -> TextProposalRequest:
    return TextProposalRequest.model_validate({"text": text, "client_now": CLIENT_NOW, **extra})


def expense_answer(**overrides: object) -> object:
    fields = {
        "intent": "expense",
        "amount": 15900,
        "description": "Almuerzo en Subway",
        "account": "Efectivo",
        "category": "Comida",
    }
    return raw(**{**fields, **overrides})


def draft_count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(TextCaptureProposalModel)) or 0


def test_a_natural_text_proposes_the_right_operation_without_saving_it(
    session_factory: sessionmaker[Session],
) -> None:
    service, expenses, incomes, transfers = build_service(
        session_factory, FakeGateway(expense_answer())
    )

    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    assert proposal.status is ProposalStatus.READY
    assert proposal.one_tap is True
    assert proposal.intent is CaptureIntent.EXPENSE
    assert (proposal.amount, proposal.currency_code) == ("15900.00", "COP")
    assert proposal.account is not None and proposal.account.name == "Efectivo"
    assert proposal.category is not None and proposal.category.name == "Comida"
    assert proposal.date == CLIENT_NOW.date()
    assert proposal.original_text == EXPENSE_TEXT
    assert (expenses.commands, incomes.requests, transfers.requests) == ([], [], [])
    assert draft_count(session_factory) == 1


def test_a_provider_failure_saves_nothing_and_fails_with_a_stable_error(
    session_factory: sessionmaker[Session],
) -> None:
    service, *_ = build_service(session_factory, FakeGateway(TextProviderError("timeout")))

    with pytest.raises(TextCaptureUnavailableError) as caught:
        asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    assert caught.value.code == "text_capture_unavailable"
    assert caught.value.status == 503
    assert draft_count(session_factory) == 0


def test_a_correction_keeps_the_original_text_and_merges_what_it_mentions(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(
        expense_answer(account=None, category=None),
        # The correction only mentions the account; the model repeats nothing else.
        raw(intent="expense", account="Nequi"),
    )
    service, *_ = build_service(session_factory, gateway)
    first = asyncio.run(service.propose(owner_id=OWNER, request=turn()))
    assert first.clarification is not None and first.clarification.field == "account"

    second = asyncio.run(
        service.propose(
            owner_id=OWNER, request=turn("mejor fue con Nequi", proposal_id=first.proposal_id)
        )
    )

    assert second.proposal_id == first.proposal_id
    assert second.revision == 2
    assert second.original_text == EXPENSE_TEXT
    assert second.follow_ups == ["mejor fue con Nequi"]
    assert second.status is ProposalStatus.READY
    assert second.account is not None and second.account.name == "Nequi"
    assert second.amount == "15900.00"
    assert second.description == "Almuerzo en Subway"
    sent = gateway.calls[1]
    assert sent["original_text"] == EXPENSE_TEXT
    assert sent["follow_ups"] == ["mejor fue con Nequi"]
    assert sent["previous"]["amount"] == "15900.00"
    assert draft_count(session_factory) == 1


def test_a_tapped_choice_completes_the_draft_without_a_new_model_call(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(expense_answer(account=None))
    service, *_ = build_service(session_factory, gateway)
    first = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    chosen = asyncio.run(
        service.propose(
            owner_id=OWNER,
            request=TextProposalRequest(
                client_now=CLIENT_NOW,
                proposal_id=first.proposal_id,
                choice=TextChoice(field="account", value="3"),
            ),
        )
    )

    assert len(gateway.calls) == 1
    assert chosen.status is ProposalStatus.READY
    assert chosen.account is not None and chosen.account.name == "Bancolombia"
    assert chosen.original_text == EXPENSE_TEXT
    assert chosen.revision == 2


def test_transfers_are_proposed_as_transfers(session_factory: sessionmaker[Session]) -> None:
    service, *_ = build_service(
        session_factory,
        FakeGateway(
            raw(
                intent="expense",
                amount=200000,
                account="Nequi",
                destination_account="Bancolombia",
            )
        ),
    )

    proposal = asyncio.run(
        service.propose(owner_id=OWNER, request=turn("pasé 200 mil de Nequi a Bancolombia"))
    )

    assert proposal.intent is CaptureIntent.TRANSFER
    assert proposal.destination_account is not None
    assert proposal.destination_account.name == "Bancolombia"
    assert proposal.category is None
    assert proposal.one_tap is True


def test_confirming_uses_the_normal_expense_operation_and_marks_the_source(
    session_factory: sessionmaker[Session],
) -> None:
    service, expenses, *_ = build_service(session_factory, FakeGateway(expense_answer()))
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    confirmation = service.confirm(
        owner_id=OWNER,
        proposal_id=proposal.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.EXPENSE,
            amount="15900.00",  # type: ignore[arg-type]
            date=proposal.date,  # type: ignore[arg-type]
            description="Almuerzo en Subway",
            account_id=1,
            category_id=10,
        ),
    )

    assert len(expenses.commands) == 1
    owner_id, command = expenses.commands[0]
    assert owner_id == OWNER
    assert command.capture_source == "text_ai"
    assert (command.account_id, command.category_id) == (1, 10)
    assert command.amount == Decimal("15900.00")
    assert command.merchant == "Almuerzo en Subway"
    assert command.occurred_at == CLIENT_NOW.astimezone(UTC)
    assert confirmation.record_id == 701 and confirmation.replayed is False
    assert service.get(owner_id=OWNER, proposal_id=proposal.proposal_id).status is (
        ProposalStatus.CONFIRMED
    )


def test_confirming_twice_records_the_operation_once(
    session_factory: sessionmaker[Session],
) -> None:
    service, expenses, *_ = build_service(session_factory, FakeGateway(expense_answer()))
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn()))
    command = TextProposalConfirmRequest(
        intent=CaptureIntent.EXPENSE,
        amount="15900.00",  # type: ignore[arg-type]
        date=proposal.date,  # type: ignore[arg-type]
        account_id=1,
    )

    first = service.confirm(owner_id=OWNER, proposal_id=proposal.proposal_id, command=command)
    second = service.confirm(owner_id=OWNER, proposal_id=proposal.proposal_id, command=command)

    assert len(expenses.commands) == 1
    assert second.replayed is True
    assert second.record_id == first.record_id


def test_edited_values_are_what_gets_recorded_for_an_income(
    session_factory: sessionmaker[Session],
) -> None:
    service, _, incomes, _ = build_service(
        session_factory,
        FakeGateway(raw(intent="income", amount=2000000, account="Bancolombia")),
    )
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn("me pagaron el sueldo")))

    service.confirm(
        owner_id=OWNER,
        proposal_id=proposal.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.INCOME,
            amount="2100000.00",  # type: ignore[arg-type]
            date=proposal.date,  # type: ignore[arg-type]
            account_id=2,
            income_source_id=20,
        ),
    )

    _, request, capture_source = incomes.requests[0]
    assert capture_source == "text_ai"
    assert (request.account_id, str(request.amount)) == (2, "2100000.00")
    assert request.income_source_id == 20


def test_confirming_a_transfer_records_both_sides_through_the_transfer_operation(
    session_factory: sessionmaker[Session],
) -> None:
    service, _, _, transfers = build_service(
        session_factory,
        FakeGateway(
            raw(intent="transfer", amount=90000, account="Nequi", destination_account="Efectivo")
        ),
    )
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn("retiré 90 mil")))

    confirmation = service.confirm(
        owner_id=OWNER,
        proposal_id=proposal.proposal_id,
        command=TextProposalConfirmRequest(
            intent=CaptureIntent.TRANSFER,
            amount="90000.00",  # type: ignore[arg-type]
            date=proposal.date,  # type: ignore[arg-type]
            account_id=2,
            destination_account_id=1,
        ),
    )

    _, request, capture_source = transfers.requests[0]
    assert capture_source == "text_ai"
    assert (request.source_account_id, request.destination_account_id) == (2, 1)
    assert confirmation.destination_account_name == "Bancolombia"


def test_a_transfer_without_destination_is_rejected_before_any_operation(
    session_factory: sessionmaker[Session],
) -> None:
    service, _, _, transfers = build_service(session_factory, FakeGateway(expense_answer()))
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    with pytest.raises(RequestInvalidError):
        service.confirm(
            owner_id=OWNER,
            proposal_id=proposal.proposal_id,
            command=TextProposalConfirmRequest(
                intent=CaptureIntent.TRANSFER,
                amount="10.00",  # type: ignore[arg-type]
                date=proposal.date,  # type: ignore[arg-type]
                account_id=1,
            ),
        )

    assert transfers.requests == []
    assert service.get(owner_id=OWNER, proposal_id=proposal.proposal_id).status is (
        ProposalStatus.READY
    )


def test_a_discarded_draft_cannot_be_corrected_or_confirmed(
    session_factory: sessionmaker[Session],
) -> None:
    service, expenses, *_ = build_service(session_factory, FakeGateway(expense_answer()))
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    service.discard(owner_id=OWNER, proposal_id=proposal.proposal_id)

    with pytest.raises(TextProposalClosedError):
        asyncio.run(
            service.propose(owner_id=OWNER, request=turn("otra", proposal_id=proposal.proposal_id))
        )
    with pytest.raises(TextProposalClosedError):
        service.confirm(
            owner_id=OWNER,
            proposal_id=proposal.proposal_id,
            command=TextProposalConfirmRequest(
                intent=CaptureIntent.EXPENSE,
                amount="1.00",  # type: ignore[arg-type]
                date=proposal.date,  # type: ignore[arg-type]
                account_id=1,
            ),
        )
    assert expenses.commands == []


def test_drafts_are_private_to_their_owner(session_factory: sessionmaker[Session]) -> None:
    service, *_ = build_service(session_factory, FakeGateway(expense_answer()))
    proposal = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    for action in (
        lambda: service.get(owner_id=999, proposal_id=proposal.proposal_id),
        lambda: service.discard(owner_id=999, proposal_id=proposal.proposal_id),
        lambda: service.get(owner_id=OWNER, proposal_id=uuid4()),
    ):
        with pytest.raises(TextProposalNotFoundError):
            action()


def test_expired_drafts_disappear_and_are_purged_on_the_next_proposal(
    session_factory: sessionmaker[Session],
) -> None:
    clock = {"now": datetime.now(UTC)}
    gateway = FakeGateway(expense_answer(), expense_answer())
    service, *_ = build_service(session_factory, gateway, clock=lambda: clock["now"])
    stale = asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    clock["now"] += timedelta(hours=25)
    with pytest.raises(TextProposalNotFoundError):
        service.get(owner_id=OWNER, proposal_id=stale.proposal_id)
    asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    assert draft_count(session_factory) == 1


def test_request_rules() -> None:
    with pytest.raises(ValueError, match="empty"):
        TextProposalRequest(text="   ", client_now=CLIENT_NOW)
    with pytest.raises(ValueError, match="timezone"):
        TextProposalRequest(text="x", client_now=datetime(2026, 10, 2, 10, 0))
    with pytest.raises(ValueError, match="proposal_id"):
        TextProposalRequest(client_now=CLIENT_NOW, choice=TextChoice(field="account", value="1"))


def test_a_missing_provider_key_is_reported_as_not_configured_not_as_an_outage(
    session_factory: sessionmaker[Session],
) -> None:
    service, *_ = build_service(session_factory, FakeGateway(TextProviderError("not_configured")))

    with pytest.raises(TextCaptureNotConfiguredError) as caught:
        asyncio.run(service.propose(owner_id=OWNER, request=turn()))

    assert caught.value.code == "text_capture_not_configured"
    assert caught.value.status == 503
    assert draft_count(session_factory) == 0


def test_a_missing_concept_is_asked_and_the_typed_answer_completes_the_draft(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(
        raw(intent="expense", amount=13000, account="Efectivo"),
        raw(intent="expense", description="Bus"),
    )
    service, *_ = build_service(session_factory, gateway)
    first = asyncio.run(service.propose(owner_id=OWNER, request=turn("13 mil pesos")))
    assert first.status is ProposalStatus.NEEDS_CLARIFICATION
    assert first.clarification is not None and first.clarification.field == "description"

    second = asyncio.run(
        service.propose(owner_id=OWNER, request=turn("bus", proposal_id=first.proposal_id))
    )

    assert second.status is ProposalStatus.READY
    assert second.description == "Bus"
    assert second.amount == "13000.00"
    assert second.original_text == "13 mil pesos"


def test_the_concept_is_not_asked_twice_even_if_the_answer_does_not_give_it(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(
        raw(intent="expense", amount=13000, account="Efectivo"),
        raw(intent="expense", account="Efectivo"),
    )
    service, *_ = build_service(session_factory, gateway)
    first = asyncio.run(service.propose(owner_id=OWNER, request=turn("13 mil pesos")))

    second = asyncio.run(
        service.propose(owner_id=OWNER, request=turn("no sé", proposal_id=first.proposal_id))
    )

    assert second.status is ProposalStatus.READY
    assert second.description is None


def test_skipping_the_concept_with_a_tapped_option_needs_no_model_call(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(raw(intent="expense", amount=13000, account="Efectivo"))
    service, *_ = build_service(session_factory, gateway)
    first = asyncio.run(service.propose(owner_id=OWNER, request=turn("13 mil pesos")))

    skipped = asyncio.run(
        service.propose(
            owner_id=OWNER,
            request=TextProposalRequest(
                client_now=CLIENT_NOW,
                proposal_id=first.proposal_id,
                choice=TextChoice(field="description", value="skip"),
            ),
        )
    )

    assert len(gateway.calls) == 1
    assert skipped.status is ProposalStatus.READY
