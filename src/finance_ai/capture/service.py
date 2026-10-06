"""Application service for HU-013: propose from text, edit by conversation, then confirm.

Nothing financial is saved while proposing. Confirming calls the same application services the
manual forms use, so every rule (ownership, active accounts, currencies, atomicity) is the
manual workflow's own rule rather than a second copy of it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from finance_ai.accounts.schemas import format_money
from finance_ai.capture.context import CaptureContext
from finance_ai.capture.gateway import TextCaptureGateway, TextProviderError
from finance_ai.capture.grounding import (
    DraftState,
    Outcome,
    apply_choice,
    finalize,
    ground,
)
from finance_ai.capture.repository import TextProposalRepository
from finance_ai.capture.schemas import (
    CAPTURE_SOURCE,
    AccountRef,
    CaptureIntent,
    CatalogRef,
    ProposalStatus,
    TextCaptureConfirmation,
    TextProposalConfirmRequest,
    TextProposalRequest,
    TextProposalResponse,
)
from finance_ai.core.errors import (
    RequestInvalidError,
    TextCaptureNotConfiguredError,
    TextCaptureUnavailableError,
    TextProposalClosedError,
    TextProposalNotFoundError,
)
from finance_ai.core.logging import get_logger
from finance_ai.expenses.schemas import CreateExpenseCommand
from finance_ai.expenses.service import ExpenseService
from finance_ai.incomes.schemas import IncomeWriteRequest
from finance_ai.incomes.service import IncomeService
from finance_ai.transfers.schemas import TransferCreateRequest
from finance_ai.transfers.service import TransferService

logger = get_logger(__name__)

DRAFT_TTL = timedelta(hours=24)
MERCHANT_MAX = 150
_FALLBACK_DESCRIPTION = {
    CaptureIntent.EXPENSE: "Gasto",
    CaptureIntent.INCOME: "Ingreso",
    CaptureIntent.TRANSFER: "Transferencia",
}
_MERGED_FIELDS = (
    "amount",
    "description",
    "account_id",
    "destination_account_id",
    "destination_amount",
    "category_id",
    "income_source_id",
)

ContextLoader = Callable[[int], CaptureContext]


class TextCaptureService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        gateway: TextCaptureGateway,
        context_loader: ContextLoader,
        expenses: ExpenseService,
        incomes: IncomeService,
        transfers: TransferService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.context_loader = context_loader
        self.expenses = expenses
        self.incomes = incomes
        self.transfers = transfers
        self.clock = clock

    # ------------------------------------------------------------------ propose

    async def propose(self, *, owner_id: int, request: TextProposalRequest) -> TextProposalResponse:
        """Reads one chat turn and returns the proposal, or the one question to ask."""
        context = await asyncio.to_thread(self.context_loader, owner_id)
        previous: TextProposalResponse | None = None
        meta: dict[str, Any] = {}
        if request.proposal_id is not None:
            previous, meta = await asyncio.to_thread(self._load_open, owner_id, request.proposal_id)

        today = request.client_now.date()
        original_text = previous.original_text if previous else request.text
        follow_ups = list(previous.follow_ups) if previous else []
        stated_currency: str | None = meta.get("stated_currency")

        if request.choice is not None and previous is not None:
            state = _state_from(previous, stated_currency, today)
            apply_choice(state, request.choice, context)
        else:
            if previous is not None:
                follow_ups.append(request.text)
            state = await self._interpret(
                context=context,
                today=today,
                original_text=original_text,
                follow_ups=follow_ups,
                previous=previous,
                stated_currency=stated_currency,
            )
        outcome = finalize(state, context)

        now = self.clock()
        response = _build_response(
            state=state,
            outcome=outcome,
            context=context,
            proposal_id=previous.proposal_id if previous else uuid4(),
            revision=previous.revision + 1 if previous else 1,
            original_text=original_text,
            follow_ups=follow_ups,
            created_at=previous.created_at if previous else now,
            expires_at=now + DRAFT_TTL,
        )
        new_meta = {
            "client_now": request.client_now.isoformat(),
            "stated_currency": state.stated_currency,
        }
        await asyncio.to_thread(self._save, owner_id, response, new_meta, previous is None)
        return response

    async def _interpret(
        self,
        *,
        context: CaptureContext,
        today: dt.date,
        original_text: str,
        follow_ups: list[str],
        previous: TextProposalResponse | None,
        stated_currency: str | None,
    ) -> DraftState:
        try:
            raw = await self.gateway.interpret(
                context=context,
                today=today,
                original_text=original_text,
                follow_ups=follow_ups,
                previous=_previous_summary(previous) if previous else None,
            )
        except TextProviderError as exc:
            if exc.kind == "not_configured":
                raise TextCaptureNotConfiguredError from exc
            raise TextCaptureUnavailableError from exc
        state = ground(
            raw,
            context,
            today=today,
            text=" ".join([original_text, *follow_ups]),
            has_follow_ups=bool(follow_ups),
        )
        if previous is not None:
            _merge_previous(state, _state_from(previous, stated_currency, today))
        return state

    # --------------------------------------------------------------- drafts

    def get(self, *, owner_id: int, proposal_id: UUID) -> TextProposalResponse:
        proposal, _ = self._load(owner_id, proposal_id)
        return proposal

    def discard(self, *, owner_id: int, proposal_id: UUID) -> None:
        with self.session_factory() as session, session.begin():
            repository = TextProposalRepository(session)
            row = repository.get(owner_id, proposal_id, now=self.clock(), for_update=True)
            if row is None:
                raise TextProposalNotFoundError
            if row.status == ProposalStatus.CONFIRMED.value:
                raise TextProposalClosedError
            proposal = TextProposalResponse.model_validate(row.proposal_data["proposal"])
            repository.update(
                row,
                proposal.model_copy(update={"status": ProposalStatus.DISCARDED}),
                dict(row.proposal_data.get("meta", {})),
            )

    def _load(
        self, owner_id: int, proposal_id: UUID
    ) -> tuple[TextProposalResponse, dict[str, Any]]:
        with self.session_factory() as session:
            row = TextProposalRepository(session).get(owner_id, proposal_id, now=self.clock())
            if row is None:
                raise TextProposalNotFoundError
            data = row.proposal_data
            return (
                TextProposalResponse.model_validate(data["proposal"]),
                dict(data.get("meta", {})),
            )

    def _load_open(
        self, owner_id: int, proposal_id: UUID
    ) -> tuple[TextProposalResponse, dict[str, Any]]:
        proposal, meta = self._load(owner_id, proposal_id)
        if proposal.status in (ProposalStatus.CONFIRMED, ProposalStatus.DISCARDED):
            raise TextProposalClosedError
        return proposal, meta

    def _save(
        self, owner_id: int, proposal: TextProposalResponse, meta: dict[str, Any], is_new: bool
    ) -> None:
        with self.session_factory() as session, session.begin():
            repository = TextProposalRepository(session)
            if is_new:
                repository.purge_expired(self.clock())
                repository.add(owner_id, proposal, meta)
                return
            row = repository.get(owner_id, proposal.proposal_id, now=self.clock(), for_update=True)
            if row is None:
                raise TextProposalNotFoundError
            repository.update(row, proposal, meta)

    # ------------------------------------------------------------- confirm

    def confirm(
        self, *, owner_id: int, proposal_id: UUID, command: TextProposalConfirmRequest
    ) -> TextCaptureConfirmation:
        """Records the operation once. Repeating the call returns the first result."""
        with self.session_factory() as session, session.begin():
            repository = TextProposalRepository(session)
            row = repository.get(owner_id, proposal_id, now=self.clock(), for_update=True)
            if row is None:
                raise TextProposalNotFoundError
            data = row.proposal_data
            if row.status == ProposalStatus.CONFIRMED.value and "confirmation" in data:
                stored = TextCaptureConfirmation.model_validate(data["confirmation"])
                return stored.model_copy(update={"replayed": True})
            if row.status == ProposalStatus.DISCARDED.value:
                raise TextProposalClosedError

            proposal = TextProposalResponse.model_validate(data["proposal"])
            meta = dict(data.get("meta", {}))
            client_now = (
                datetime.fromisoformat(meta["client_now"]) if "client_now" in meta else None
            )
            confirmation = self._execute(
                owner_id=owner_id,
                proposal_id=proposal_id,
                command=command,
                client_now=client_now or self.clock(),
            )
            repository.update(
                row,
                proposal.model_copy(update={"status": ProposalStatus.CONFIRMED}),
                meta,
                confirmation=confirmation.model_dump(mode="json"),
            )
        logger.info(
            "Text capture confirmed",
            extra={
                "event": "capture.confirmed",
                "outcome": "success",
                "operation": command.intent.value,
            },
        )
        return confirmation

    def _execute(
        self,
        *,
        owner_id: int,
        proposal_id: UUID,
        command: TextProposalConfirmRequest,
        client_now: datetime,
    ) -> TextCaptureConfirmation:
        if command.intent is CaptureIntent.EXPENSE:
            expense = self.expenses.create(
                owner_id=owner_id,
                command=CreateExpenseCommand(
                    account_id=command.account_id,
                    amount=command.amount,
                    merchant=(command.description or _FALLBACK_DESCRIPTION[command.intent])[
                        :MERCHANT_MAX
                    ],
                    occurred_at=_occurred_at(command.date, client_now),
                    category_id=command.category_id,
                    capture_source=CAPTURE_SOURCE,
                ),
            )
            return TextCaptureConfirmation(
                proposal_id=proposal_id,
                intent=command.intent,
                record_id=expense.id,
                amount=format_money(command.amount),
                currency_code=expense.currency.code,
                date=command.date,
                description=expense.store,
                account_name=expense.account_name,
            )
        if command.intent is CaptureIntent.INCOME:
            income = self.incomes.create(
                owner_id=owner_id,
                request=IncomeWriteRequest(
                    account_id=command.account_id,
                    amount=format_money(command.amount),
                    date=command.date,
                    description=command.description,
                    income_source_id=command.income_source_id,
                ),
                capture_source=CAPTURE_SOURCE,
            )
            return TextCaptureConfirmation(
                proposal_id=proposal_id,
                intent=command.intent,
                record_id=income.id,
                amount=format_money(income.amount),
                currency_code=income.currency_code,
                date=income.date,
                description=income.description,
                account_name=income.account_name,
            )
        if command.destination_account_id is None:
            raise RequestInvalidError(
                {"destination_account_id": ["Choose the account that received the money."]}
            )
        transfer = self.transfers.create(
            owner_id=owner_id,
            request=TransferCreateRequest(
                source_account_id=command.account_id,
                destination_account_id=command.destination_account_id,
                source_amount=format_money(command.amount),
                destination_amount=(
                    None
                    if command.destination_amount is None
                    else format_money(command.destination_amount)
                ),
                date=command.date,
                description=command.description,
            ),
            capture_source=CAPTURE_SOURCE,
        )
        return TextCaptureConfirmation(
            proposal_id=proposal_id,
            intent=command.intent,
            record_id=transfer.id,
            amount=format_money(transfer.source_amount),
            currency_code=transfer.source_account.currency_code,
            date=transfer.date,
            description=transfer.description,
            account_name=transfer.source_account.name,
            destination_account_name=transfer.destination_account.name,
        )


def _occurred_at(day: dt.date, client_now: datetime) -> datetime:
    """Today keeps the real moment; another day is recorded at noon in the user's timezone."""
    if day == client_now.date():
        return client_now.astimezone(UTC)
    local_noon = datetime.combine(day, dt.time(12, 0), tzinfo=client_now.tzinfo)
    return local_noon.astimezone(UTC)


def _previous_summary(proposal: TextProposalResponse) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "intent": proposal.intent.value if proposal.intent else None,
        "amount": proposal.amount,
        "currency": proposal.currency_code,
        "date": proposal.date.isoformat() if proposal.date else None,
        "description": proposal.description,
        "account": proposal.account.name if proposal.account else None,
        "destination_account": (
            proposal.destination_account.name if proposal.destination_account else None
        ),
        "destination_amount": proposal.destination_amount,
        "category": proposal.category.name if proposal.category else None,
        "income_source": proposal.income_source.name if proposal.income_source else None,
    }
    return {key: value for key, value in summary.items() if value is not None}


def _state_from(
    proposal: TextProposalResponse, stated_currency: str | None, today: dt.date
) -> DraftState:
    return DraftState(
        date=proposal.date or today,
        intent=proposal.intent,
        amount=Decimal(proposal.amount) if proposal.amount else None,
        description=proposal.description,
        account_id=proposal.account.id if proposal.account else None,
        destination_account_id=(
            proposal.destination_account.id if proposal.destination_account else None
        ),
        destination_amount=(
            Decimal(proposal.destination_amount) if proposal.destination_amount else None
        ),
        category_id=proposal.category.id if proposal.category else None,
        income_source_id=proposal.income_source.id if proposal.income_source else None,
        confidence=proposal.confidence,
        stated_currency=stated_currency,
        description_asked=(
            proposal.clarification is not None and proposal.clarification.field == "description"
        ),
        date_stated=proposal.date is not None,
    )


def _merge_previous(state: DraftState, previous: DraftState) -> None:
    """A correction only changes what it mentions: anything it leaves out is kept."""
    for name in _MERGED_FIELDS:
        if getattr(state, name) is None and getattr(previous, name) is not None:
            setattr(state, name, getattr(previous, name))
    # A question already asked about the concept is not asked a second time.
    state.description_asked = state.description_asked or previous.description_asked
    if not state.date_stated:
        state.date = previous.date
    if state.intent is None:
        state.intent = previous.intent


def _build_response(
    *,
    state: DraftState,
    outcome: Outcome,
    context: CaptureContext,
    proposal_id: UUID,
    revision: int,
    original_text: str,
    follow_ups: list[str],
    created_at: datetime,
    expires_at: datetime,
) -> TextProposalResponse:
    account = context.account(state.account_id)
    destination = context.account(state.destination_account_id)
    category = context.category(state.category_id)
    source = context.income_source(state.income_source_id)
    is_transfer = state.intent is CaptureIntent.TRANSFER
    return TextProposalResponse(
        proposal_id=proposal_id,
        revision=revision,
        status=outcome.status,
        intent=state.intent,
        confidence=state.confidence,
        one_tap=outcome.one_tap,
        original_text=original_text,
        follow_ups=follow_ups,
        message=outcome.message,
        amount=None if state.amount is None else format_money(state.amount),
        currency_code=outcome.currency_code or (account.currency_code if account else None),
        destination_amount=(
            format_money(state.destination_amount)
            if is_transfer and state.destination_amount is not None
            else None
        ),
        date=state.date,
        description=state.description,
        account=(
            AccountRef(id=account.id, name=account.name, currency_code=account.currency_code)
            if account
            else None
        ),
        destination_account=(
            AccountRef(
                id=destination.id, name=destination.name, currency_code=destination.currency_code
            )
            if destination and is_transfer
            else None
        ),
        category=(
            CatalogRef(id=category.id, name=category.name)
            if category and state.intent is CaptureIntent.EXPENSE
            else None
        ),
        income_source=(
            CatalogRef(id=source.id, name=source.name)
            if source and state.intent is CaptureIntent.INCOME
            else None
        ),
        clarification=outcome.clarification,
        notes=list(state.notes),
        created_at=created_at,
        expires_at=expires_at,
    )
