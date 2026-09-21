import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.catalogs import Category, Subcategory
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency


class SpendingLimit(Base):
    __tablename__ = "spendinglimit"
    __table_args__ = (
        CheckConstraint("amount > 0", name="spendinglimit_amount_positive"),
        CheckConstraint("period IN ('weekly', 'monthly')", name="spendinglimit_period_valid"),
        CheckConstraint(
            "(subcategory_id IS NOT NULL AND category_id IS NULL) "
            "OR (subcategory_id IS NULL AND category_id IS NOT NULL)",
            name="spendinglimit_xor_target",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    subcategory_id: Mapped[int | None] = mapped_column(
        ForeignKey("subcategory.id", ondelete="CASCADE")
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))

    user: Mapped[AppUser] = relationship()
    subcategory: Mapped[Subcategory | None] = relationship()
    category: Mapped[Category | None] = relationship()


Index("idx_limit_user_id", SpendingLimit.user_id)


class Budget(Base):
    __tablename__ = "budget"
    __table_args__ = (
        CheckConstraint("amount > 0", name="budget_amount_positive"),
        CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="budget_date_order"),
        CheckConstraint(
            "frequency_interval IS NULL OR frequency_interval > 0",
            name="budget_interval_positive",
        ),
        CheckConstraint(
            "("
            "(schedule_type = 'recurring' AND frequency_unit IS NOT NULL "
            "AND frequency_interval IS NOT NULL) "
            "OR (schedule_type = 'one_time' AND frequency_unit IS NULL "
            "AND frequency_interval IS NULL)"
            ")",
            name="budget_schedule_fields",
        ),
        Index("ix_budget_user_active", "user_id", "is_active"),
        Index("ix_budget_user_dates", "user_id", "starts_on", "ends_on"),
        Index("ix_budget_category_active", "category_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    currency_id: Mapped[int] = mapped_column(
        ForeignKey("currency.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("category.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    frequency_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    frequency_interval: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    starts_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[AppUser] = relationship()
    currency: Mapped[Currency] = relationship()
    category: Mapped[Category | None] = relationship()
