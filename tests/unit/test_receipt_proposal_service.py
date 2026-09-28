"""Unit tests for the receipt proposal application service (HU-015 draft lifecycle)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from finance_ai.core.errors import ReceiptProposalNotFoundError
from finance_ai.receipts.models import ReceiptProposalModel
from finance_ai.receipts.schemas import ReceiptProposalResponse
from finance_ai.receipts.service import ReceiptProposalService

NOW = datetime.now(UTC)


def make_proposal(**overrides: Any) -> ReceiptProposalResponse:
    data: dict[str, Any] = {
        "proposal_id": uuid4(),
        "status": "complete",
        "merchant_name": "Éxito Calle 80",
        "purchase_date": "2026-09-27",
        "total_amount": Decimal("11700.00"),
        "currency": "COP",
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    data.update(overrides)
    return ReceiptProposalResponse(**data)


class FakeProposalRepository:
    def __init__(self, rows: dict[UUID, ReceiptProposalModel] | None = None) -> None:
        self.rows: dict[UUID, ReceiptProposalModel] = rows or {}
        self.created: list[tuple[int, ReceiptProposalResponse]] = []
        self.updated: list[tuple[int, UUID, ReceiptProposalResponse]] = []
        self.discarded: list[tuple[int, UUID]] = []

    def create(self, user_id: int, proposal: ReceiptProposalResponse) -> ReceiptProposalModel:
        self.created.append((user_id, proposal))
        row = ReceiptProposalModel(
            id=proposal.proposal_id,
            user_id=user_id,
            status=proposal.status,
            proposal_data=proposal.model_dump(mode="json"),
            revision=proposal.revision,
            created_at=proposal.created_at,
            expires_at=proposal.expires_at,
        )
        self.rows[proposal.proposal_id] = row
        return row

    def get_by_id_for_user(
        self, user_id: int, proposal_id: UUID, *, for_update: bool = False
    ) -> ReceiptProposalModel | None:
        row = self.rows.get(proposal_id)
        if row is not None and row.user_id == user_id:
            return row
        return None

    def update(
        self, user_id: int, proposal_id: UUID, proposal: ReceiptProposalResponse
    ) -> ReceiptProposalModel | None:
        row = self.get_by_id_for_user(user_id, proposal_id)
        if row is None:
            return None
        row.proposal_data = proposal.model_dump(mode="json")
        row.status = proposal.status
        row.revision = proposal.revision
        self.updated.append((user_id, proposal_id, proposal))
        return row

    def discard(self, user_id: int, proposal_id: UUID) -> bool:
        row = self.get_by_id_for_user(user_id, proposal_id)
        if row is None:
            return False
        row.status = "discarded"
        self.discarded.append((user_id, proposal_id))
        return True


class FakeUnitOfWork:
    def __init__(self, proposals: FakeProposalRepository) -> None:
        self.proposals = proposals
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def flush(self) -> None: ...

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def make_service(
    *, duplicate: bool = False, failing_lookup: bool = False
) -> tuple[ReceiptProposalService, FakeProposalRepository, list[FakeUnitOfWork]]:
    repository = FakeProposalRepository()
    units: list[FakeUnitOfWork] = []

    def uow_factory() -> FakeUnitOfWork:
        unit = FakeUnitOfWork(repository)
        units.append(unit)
        return unit

    def duplicate_lookup(*_args: Any, **_kwargs: Any) -> bool:
        if failing_lookup:
            raise RuntimeError("duplicate lookup unavailable")
        return duplicate

    service = ReceiptProposalService(uow_factory=uow_factory, duplicate_lookup=duplicate_lookup)
    return service, repository, units


def test_create_draft_commits_and_returns_owner_scoped_proposal() -> None:
    service, repository, units = make_service()
    proposal = make_proposal()

    stored = service.create_draft(owner_id=100, proposal=proposal)

    assert stored.proposal_id == proposal.proposal_id
    assert repository.created == [(100, proposal)]
    assert units[0].committed is True


def test_create_draft_flags_a_potential_duplicate() -> None:
    service, _repository, _units = make_service(duplicate=True)

    stored = service.create_draft(owner_id=100, proposal=make_proposal())

    assert stored.possible_duplicate is True
    assert stored.duplicate_warning is not None
    assert "Éxito Calle 80" in stored.duplicate_warning
    assert any(issue.code == "possible_duplicate" for issue in stored.issues)


def test_create_draft_survives_a_failing_duplicate_lookup() -> None:
    service, _repository, units = make_service(failing_lookup=True)

    stored = service.create_draft(owner_id=100, proposal=make_proposal())

    assert stored.possible_duplicate is False
    assert units[0].committed is True


def test_create_draft_skips_the_lookup_without_merchant_or_total() -> None:
    calls: list[tuple[Any, ...]] = []

    def lookup(*args: Any) -> bool:
        calls.append(args)
        return True

    repository = FakeProposalRepository()

    def uow_factory() -> FakeUnitOfWork:
        return FakeUnitOfWork(repository)

    service = ReceiptProposalService(uow_factory=uow_factory, duplicate_lookup=lookup)
    stored = service.create_draft(
        owner_id=100, proposal=make_proposal(merchant_name=None, total_amount=None)
    )

    assert calls == []
    assert stored.possible_duplicate is False


def test_get_draft_returns_the_stored_proposal() -> None:
    service, repository, _units = make_service()
    proposal = make_proposal()
    service.create_draft(owner_id=100, proposal=proposal)

    fetched = service.get_draft(owner_id=100, proposal_id=proposal.proposal_id)

    assert fetched.proposal_id == proposal.proposal_id
    assert fetched.merchant_name == proposal.merchant_name
    assert repository.rows[proposal.proposal_id].user_id == 100


def test_get_draft_rejects_a_foreign_owner_and_a_missing_draft() -> None:
    service, _repository, _units = make_service()
    proposal = make_proposal()
    service.create_draft(owner_id=100, proposal=proposal)

    with pytest.raises(ReceiptProposalNotFoundError):
        service.get_draft(owner_id=202, proposal_id=proposal.proposal_id)
    with pytest.raises(ReceiptProposalNotFoundError):
        service.get_draft(owner_id=100, proposal_id=uuid4())


def test_replace_draft_updates_and_commits() -> None:
    service, repository, units = make_service()
    proposal = make_proposal()
    service.create_draft(owner_id=100, proposal=proposal)
    rotated = proposal.model_copy(update={"revision": 2, "merchant_name": "Éxito Rotado"})

    stored = service.replace_draft(owner_id=100, proposal_id=proposal.proposal_id, proposal=rotated)

    assert stored.revision == 2
    assert stored.merchant_name == "Éxito Rotado"
    assert repository.updated[0][1] == proposal.proposal_id
    assert units[-1].committed is True


def test_replace_draft_raises_for_a_missing_draft() -> None:
    service, _repository, _units = make_service()

    with pytest.raises(ReceiptProposalNotFoundError):
        service.replace_draft(owner_id=100, proposal_id=uuid4(), proposal=make_proposal())


def test_discard_draft_marks_and_commits() -> None:
    service, repository, units = make_service()
    proposal = make_proposal()
    service.create_draft(owner_id=100, proposal=proposal)

    service.discard_draft(owner_id=100, proposal_id=proposal.proposal_id)

    assert repository.discarded == [(100, proposal.proposal_id)]
    assert units[-1].committed is True


def test_discard_draft_raises_for_a_missing_draft() -> None:
    service, _repository, _units = make_service()

    with pytest.raises(ReceiptProposalNotFoundError):
        service.discard_draft(owner_id=100, proposal_id=uuid4())
