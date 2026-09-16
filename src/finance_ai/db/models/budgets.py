from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.catalogs import Category, Subcategory
from finance_ai.db.models.identity import AppUser


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
