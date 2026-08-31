from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base
from finance_ai.db.models.reference import Currency


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (CheckConstraint("email = lower(btrim(email))", name="email_normalized"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    preferred_currency_id: Mapped[int] = mapped_column(
        ForeignKey("currency.id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    preferred_currency: Mapped[Currency] = relationship(lazy="joined")


Index("uq_app_user_email", func.lower(AppUser.email), unique=True)
