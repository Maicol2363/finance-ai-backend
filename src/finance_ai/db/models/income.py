import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.catalogs import Incomesource
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency


class Income(Base):
    __tablename__ = "income"
    __table_args__ = (CheckConstraint("amount > 0", name="income_amount_positive"),)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    income_source_id: Mapped[int] = mapped_column(
        ForeignKey("incomesource.id", ondelete="RESTRICT"), nullable=False
    )
    currency_id: Mapped[int] = mapped_column(
        ForeignKey("currency.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    user: Mapped[AppUser] = relationship()
    income_source: Mapped[Incomesource] = relationship()
    currency: Mapped[Currency] = relationship()


Index("idx_income_user_id", Income.user_id)
Index("idx_income_date", Income.date)
Index("idx_income_source", Income.income_source_id)
