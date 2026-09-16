import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.catalogs import PaymentMethod, Subcategory
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency


class Receipt(Base):
    __tablename__ = "receipt"
    __table_args__ = (CheckConstraint("total > 0", name="receipt_total_positive"),)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    store: Mapped[str] = mapped_column(String(150), nullable=False)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    currency_id: Mapped[int] = mapped_column(
        ForeignKey("currency.id", ondelete="RESTRICT"), nullable=False
    )
    purchase_reason: Mapped[str | None] = mapped_column(Text)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("paymentmethod.id", ondelete="RESTRICT"), nullable=False
    )
    image: Mapped[str | None] = mapped_column(Text)

    user: Mapped[AppUser] = relationship()
    currency: Mapped[Currency] = relationship()
    payment_method: Mapped[PaymentMethod] = relationship()
    expenses: Mapped[list[Expense]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


class Expense(Base):
    __tablename__ = "expense"
    __table_args__ = (CheckConstraint("amount > 0", name="expense_amount_positive"),)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), nullable=False
    )
    subcategory_id: Mapped[int] = mapped_column(
        ForeignKey("subcategory.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    item_reason: Mapped[str | None] = mapped_column(Text)

    user: Mapped[AppUser] = relationship()
    receipt: Mapped[Receipt] = relationship(back_populates="expenses")
    subcategory: Mapped[Subcategory] = relationship()


Index("idx_receipt_user_id", Receipt.user_id)
Index("idx_receipt_date", Receipt.date)
Index("idx_receipt_payment", Receipt.payment_method_id)

Index("idx_expense_user_id", Expense.user_id)
Index("idx_expense_receipt", Expense.receipt_id)
Index("idx_expense_subcat", Expense.subcategory_id)
