from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finance_ai.db.base import Base


class FinancialTransaction(Base):
    __tablename__ = "financial_transaction"
    __table_args__ = (
        CheckConstraint(
            "type IN ('expense', 'income', 'transfer', 'adjustment')",
            name="type_supported",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'voided')",
            name="status_supported",
        ),
        CheckConstraint(
            "capture_source IN ("
            "'manual', 'ai', 'text_ai', 'audio_ai', 'receipt_photo_ai', 'import'"
            ")",
            name="capture_source_supported",
        ),
        CheckConstraint(
            "type = 'income' OR income_source_id IS NULL",
            name="income_source_only_for_income",
        ),
        UniqueConstraint("id", "user_id", name="uq_financial_transaction_id_user_id"),
        UniqueConstraint(
            "user_id", "source_proposal_id", name="uq_financial_transaction_source_proposal"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    income_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("income_source.id", ondelete="SET NULL")
    )
    source_proposal_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    transaction_type: Mapped[str] = mapped_column("type", String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    capture_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AccountMovement(Base):
    __tablename__ = "account_movement"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        Index(
            "ix_account_movement_user_account",
            "user_id",
            "account_id",
            "transaction_id",
        ),
        ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["financial_transaction.id", "financial_transaction.user_id"],
            name="fk_account_movement_transaction_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["account.id", "account.user_id"],
            name="fk_account_movement_account_owner",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transaction_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "ix_financial_transaction_user_status",
    FinancialTransaction.user_id,
    FinancialTransaction.status,
    FinancialTransaction.id,
)
Index(
    "ix_financial_transaction_user_occurred",
    FinancialTransaction.user_id,
    FinancialTransaction.occurred_at.desc(),
    FinancialTransaction.id.desc(),
)
Index(
    "ix_financial_transaction_income_source",
    FinancialTransaction.income_source_id,
)
Index(
    "ix_account_movement_account_transaction",
    AccountMovement.account_id,
    AccountMovement.transaction_id,
)
Index(
    "ix_account_movement_transaction_id",
    AccountMovement.transaction_id,
)
