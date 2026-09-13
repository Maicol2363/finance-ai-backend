from collections.abc import Callable

from sqlalchemy.exc import IntegrityError

from finance_ai.core.errors import (
    IncomeSourceNameTakenError,
    IncomeSourceNotFoundError,
    IncomeSourceValidationError,
)
from finance_ai.income_sources.repositories import IncomeSourceRecord
from finance_ai.income_sources.uow import IncomeSourceUnitOfWork

MAX_NAME_LENGTH = 100

UnitOfWorkFactory = Callable[[], IncomeSourceUnitOfWork]


class IncomeSourceService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

    def list(self, *, user_id: int) -> list[IncomeSourceRecord]:
        with self.uow_factory() as uow:
            return uow.income_sources.list_for_user(user_id=user_id)

    def create(self, *, user_id: int, name: str) -> IncomeSourceRecord:
        normalized = _normalize_name(name)
        try:
            with self.uow_factory() as uow:
                if uow.income_sources.name_exists(
                    user_id=user_id, normalized_name=normalized.lower()
                ):
                    raise IncomeSourceNameTakenError
                record = uow.income_sources.add(user_id=user_id, name=normalized)
                uow.commit()
                return record
        except IntegrityError as exc:
            if _is_income_source_unique_violation(exc):
                raise IncomeSourceNameTakenError from exc
            raise

    def rename(self, *, user_id: int, income_source_id: int, name: str) -> IncomeSourceRecord:
        normalized = _normalize_name(name)
        try:
            with self.uow_factory() as uow:
                if (
                    uow.income_sources.find_by_id_for_user(
                        user_id=user_id, income_source_id=income_source_id
                    )
                    is None
                ):
                    raise IncomeSourceNotFoundError
                if uow.income_sources.name_exists(
                    user_id=user_id,
                    normalized_name=normalized.lower(),
                    exclude_id=income_source_id,
                ):
                    raise IncomeSourceNameTakenError
                record = uow.income_sources.rename(
                    income_source_id=income_source_id, name=normalized
                )
                uow.commit()
                return record
        except IntegrityError as exc:
            if _is_income_source_unique_violation(exc):
                raise IncomeSourceNameTakenError from exc
            raise

    def delete(self, *, user_id: int, income_source_id: int) -> None:
        with self.uow_factory() as uow:
            if (
                uow.income_sources.find_by_id_for_user(
                    user_id=user_id, income_source_id=income_source_id
                )
                is None
            ):
                raise IncomeSourceNotFoundError
            uow.income_sources.clear_historical_references(
                user_id=user_id, income_source_id=income_source_id
            )
            uow.income_sources.delete(income_source_id=income_source_id)
            uow.commit()


def _normalize_name(raw_name: str) -> str:
    normalized = raw_name.strip()
    if not normalized:
        raise IncomeSourceValidationError("Enter an income source name.")
    if len(normalized) > MAX_NAME_LENGTH:
        raise IncomeSourceValidationError(
            f"Income source names must be {MAX_NAME_LENGTH} characters or fewer."
        )
    return normalized


def _is_income_source_unique_violation(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return constraint_name == "uq_income_source_user_name"
