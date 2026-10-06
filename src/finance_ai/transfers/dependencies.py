from finance_ai.db.session import get_session_factory
from finance_ai.transfers.service import TransferService


def get_transfer_service() -> TransferService:
    return TransferService(get_session_factory())
