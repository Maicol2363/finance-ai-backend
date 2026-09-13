from collections.abc import Iterator

import pytest

from finance_ai.core.errors import (
    IncomeSourceNameTakenError,
    IncomeSourceNotFoundError,
    IncomeSourceValidationError,
)
from finance_ai.income_sources.repositories import IncomeSourceRecord
from finance_ai.income_sources.service import IncomeSourceService


class FakeIncomeSourceRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], IncomeSourceRecord] = {}
        self.cleared: list[int] = []
        self._next_id = 1

    def list_for_user(self, *, user_id: int) -> list[IncomeSourceRecord]:
        return sorted(
            (record for (owner, _), record in self.rows.items() if owner == user_id),
            key=lambda record: record.name.lower(),
        )

    def find_by_id_for_user(
        self, *, user_id: int, income_source_id: int
    ) -> IncomeSourceRecord | None:
        return self.rows.get((user_id, income_source_id))

    def name_exists(
        self, *, user_id: int, normalized_name: str, exclude_id: int | None = None
    ) -> bool:
        return any(
            owner == user_id and record.name.lower() == normalized_name and record.id != exclude_id
            for (owner, _), record in self.rows.items()
        )

    def add(self, *, user_id: int, name: str) -> IncomeSourceRecord:
        record = IncomeSourceRecord(id=self._next_id, name=name)
        self.rows[(user_id, self._next_id)] = record
        self._next_id += 1
        return record

    def rename(self, *, income_source_id: int, name: str) -> IncomeSourceRecord:
        for (owner, source_id), _ in list(self.rows.items()):
            if source_id == income_source_id:
                updated = IncomeSourceRecord(id=source_id, name=name)
                self.rows[(owner, source_id)] = updated
                return updated
        raise LookupError("missing")

    def delete(self, *, income_source_id: int) -> None:
        for key in [key for key in self.rows if key[1] == income_source_id]:
            del self.rows[key]

    def clear_historical_references(self, *, user_id: int, income_source_id: int) -> None:
        self.cleared.append(income_source_id)


class FakeUnitOfWork:
    def __init__(self, repository: FakeIncomeSourceRepository) -> None:
        self.income_sources = repository
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        if not self.committed:
            self.rolled_back = True

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def repository() -> FakeIncomeSourceRepository:
    return FakeIncomeSourceRepository()


@pytest.fixture
def service(repository: FakeIncomeSourceRepository) -> Iterator[IncomeSourceService]:
    yield IncomeSourceService(uow_factory=lambda: FakeUnitOfWork(repository))


def test_create_trims_name_and_commits(service: IncomeSourceService) -> None:
    record = service.create(user_id=1, name="  Salary  ")
    assert record.name == "Salary"
    assert [item.name for item in service.list(user_id=1)] == ["Salary"]


def test_list_only_returns_own_rows(service: IncomeSourceService) -> None:
    service.create(user_id=1, name="Salary")
    service.create(user_id=2, name="Rent")
    assert [item.name for item in service.list(user_id=1)] == ["Salary"]


@pytest.mark.parametrize("duplicate", ["salary", "  SALARY ", "Salary"])
def test_create_rejects_case_insensitive_duplicate(
    service: IncomeSourceService, duplicate: str
) -> None:
    service.create(user_id=1, name="Salary")
    with pytest.raises(IncomeSourceNameTakenError):
        service.create(user_id=1, name=duplicate)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_create_rejects_blank_name(service: IncomeSourceService, blank: str) -> None:
    with pytest.raises(IncomeSourceValidationError):
        service.create(user_id=1, name=blank)


def test_create_rejects_overlong_name(service: IncomeSourceService) -> None:
    with pytest.raises(IncomeSourceValidationError):
        service.create(user_id=1, name="x" * 101)


def test_rename_updates_and_rejects_duplicate(service: IncomeSourceService) -> None:
    first = service.create(user_id=1, name="Salary")
    service.create(user_id=1, name="Freelance")

    renamed = service.rename(user_id=1, income_source_id=first.id, name="  Salary 2 ")
    assert renamed.name == "Salary 2"

    with pytest.raises(IncomeSourceNameTakenError):
        service.rename(user_id=1, income_source_id=first.id, name="freelance")


def test_rename_unknown_or_foreign_id_is_not_found(service: IncomeSourceService) -> None:
    record = service.create(user_id=1, name="Salary")
    with pytest.raises(IncomeSourceNotFoundError):
        service.rename(user_id=2, income_source_id=record.id, name="Other")
    with pytest.raises(IncomeSourceNotFoundError):
        service.rename(user_id=1, income_source_id=9999, name="Other")


def test_delete_is_permanent_and_frees_the_name(
    service: IncomeSourceService, repository: FakeIncomeSourceRepository
) -> None:
    record = service.create(user_id=1, name="Salary")
    service.delete(user_id=1, income_source_id=record.id)

    assert service.list(user_id=1) == []
    assert repository.cleared == [record.id]
    assert service.create(user_id=1, name="salary").name == "salary"


def test_delete_unknown_or_foreign_id_is_not_found(
    service: IncomeSourceService, repository: FakeIncomeSourceRepository
) -> None:
    record = service.create(user_id=1, name="Salary")
    with pytest.raises(IncomeSourceNotFoundError):
        service.delete(user_id=2, income_source_id=record.id)
    assert repository.cleared == []
    with pytest.raises(IncomeSourceNotFoundError):
        service.delete(user_id=1, income_source_id=9999)


def test_failed_create_rolls_back(
    service: IncomeSourceService, repository: FakeIncomeSourceRepository
) -> None:
    service.create(user_id=1, name="Salary")
    before = dict(repository.rows)
    with pytest.raises(IncomeSourceNameTakenError):
        service.create(user_id=1, name="salary")
    assert repository.rows == before
