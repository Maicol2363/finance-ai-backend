from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from finance_ai.transfers.repository import TransferRepository
from finance_ai.transfers.schemas import TransferCreateRequest
from finance_ai.transfers.service import TransferService


def test_transfer_validation_locks_accounts_in_ascending_id_order() -> None:
    repository = Mock(spec=TransferRepository)
    repository.owned_account.side_effect = lambda *, owner_id, account_id: SimpleNamespace(
        id=account_id, is_active=True
    )
    repository.account_currency_code.return_value = "COP"
    request = TransferCreateRequest(
        source_account_id=9,
        destination_account_id=3,
        source_amount="12.00",
        date=date(2026, 10, 19),
    )

    amount = TransferService._validate_references(repository, owner_id=42, request=request)

    locked_ids = [call.kwargs["account_id"] for call in repository.owned_account.call_args_list]
    assert locked_ids == [3, 9]
    assert amount == Decimal("12.00")
