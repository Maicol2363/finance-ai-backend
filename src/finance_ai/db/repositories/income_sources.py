from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_ai.db.models.catalogs import IncomeSource
from finance_ai.income_sources.repositories import IncomeSourceRecord


class SQLAlchemyIncomeSourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_user(self, *, user_id: int) -> list[IncomeSourceRecord]:
        rows = self.session.scalars(
            select(IncomeSource)
            .where(IncomeSource.user_id == user_id)
            .order_by(func.lower(IncomeSource.name), IncomeSource.id)
        ).all()
        return [IncomeSourceRecord(id=row.id, name=row.name) for row in rows]

    def find_by_id_for_user(
        self, *, user_id: int, income_source_id: int
    ) -> IncomeSourceRecord | None:
        row = self.session.scalar(
            select(IncomeSource).where(
                IncomeSource.id == income_source_id,
                IncomeSource.user_id == user_id,
            )
        )
        if row is None:
            return None
        return IncomeSourceRecord(id=row.id, name=row.name)

    def name_exists(
        self, *, user_id: int, normalized_name: str, exclude_id: int | None = None
    ) -> bool:
        statement = select(IncomeSource.id).where(
            IncomeSource.user_id == user_id,
            func.lower(IncomeSource.name) == normalized_name,
        )
        if exclude_id is not None:
            statement = statement.where(IncomeSource.id != exclude_id)
        return self.session.scalar(statement.limit(1)) is not None

    def add(self, *, user_id: int, name: str) -> IncomeSourceRecord:
        row = IncomeSource(user_id=user_id, name=name, is_active=True)
        self.session.add(row)
        self.session.flush()
        return IncomeSourceRecord(id=row.id, name=row.name)

    def rename(self, *, income_source_id: int, name: str) -> IncomeSourceRecord:
        row = self.session.get(IncomeSource, income_source_id)
        if row is None:
            raise LookupError("Income source disappeared during rename")
        row.name = name
        self.session.flush()
        return IncomeSourceRecord(id=row.id, name=row.name)

    def delete(self, *, income_source_id: int) -> None:
        row = self.session.get(IncomeSource, income_source_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def clear_historical_references(self, *, user_id: int, income_source_id: int) -> None:
        # No referencing table exists yet. The future financial_transaction.income_source_id
        # FK is ON DELETE SET NULL; this is the documented extension point for that cleanup.
        return None
