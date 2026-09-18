import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.catalogs import Category, PaymentMethod, Subcategory
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import FinancialTransaction


class Receipt(Base):
    __tablename__ = "receipt"
    __table_args__ = (CheckConstraint("total > 0", name="receipt_total_positive"),)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_transaction.id", ondelete="CASCADE")
    )
    store: Mapped[str] = mapped_column(String(150), nullable=False)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    currency_id: Mapped[int] = mapped_column(
        ForeignKey("currency.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"))
    purchase_reason: Mapped[str | None] = mapped_column(Text)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("paymentmethod.id", ondelete="RESTRICT"), nullable=False
    )
    image: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[AppUser] = relationship()
    transaction: Mapped[FinancialTransaction | None] = relationship()
    currency: Mapped[Currency] = relationship()
    payment_method: Mapped[PaymentMethod] = relationship()
    category: Mapped[Category | None] = relationship()
    expenses: Mapped[list[Expense]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


class Expense(Base):
    __tablename__ = "expense"
    __table_args__ = (
        CheckConstraint("amount > 0", name="expense_amount_positive"),
        CheckConstraint("position > 0", name="expense_position_positive"),
        CheckConstraint("quantity > 0", name="expense_quantity_positive"),
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0", name="expense_unit_price_non_negative"
        ),
        UniqueConstraint("receipt_id", "position", name="uq_expense_receipt_position"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), nullable=False
    )
    subcategory_id: Mapped[int | None] = mapped_column(
        ForeignKey("subcategory.id", ondelete="RESTRICT"), nullable=True
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, server_default=text("1")
    )
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    item_reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[AppUser] = relationship()
    receipt: Mapped[Receipt] = relationship(back_populates="expenses")
    category: Mapped[Category | None] = relationship()
    subcategory: Mapped[Subcategory | None] = relationship()


Index("idx_receipt_user_id", Receipt.user_id)
Index("idx_receipt_date", Receipt.date)
Index("idx_receipt_payment", Receipt.payment_method_id)
Index("uq_receipt_transaction_id", Receipt.transaction_id, unique=True)
Index("idx_receipt_category_date", Receipt.category_id, Receipt.date)

Index("idx_expense_user_id", Expense.user_id)
Index("idx_expense_receipt", Expense.receipt_id)
Index("idx_expense_subcat", Expense.subcategory_id)
Index("ix_expense_category", Expense.category_id)
