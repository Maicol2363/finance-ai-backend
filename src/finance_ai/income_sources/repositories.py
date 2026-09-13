from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IncomeSourceRecord:
    id: int
    name: str


class IncomeSourceRepository(Protocol):
    def list_for_user(self, *, user_id: int) -> list[IncomeSourceRecord]: ...

    def find_by_id_for_user(
        self, *, user_id: int, income_source_id: int
    ) -> IncomeSourceRecord | None: ...

    def name_exists(
        self, *, user_id: int, normalized_name: str, exclude_id: int | None = None
    ) -> bool: ...

    def add(self, *, user_id: int, name: str) -> IncomeSourceRecord: ...

    def rename(self, *, income_source_id: int, name: str) -> IncomeSourceRecord: ...

    def delete(self, *, income_source_id: int) -> None: ...

    def clear_historical_references(self, *, user_id: int, income_source_id: int) -> None: ...
