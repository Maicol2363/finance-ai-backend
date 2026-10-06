from datetime import datetime

from sqlalchemy import (
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

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.reference import Currency


class Role(Base):
    __tablename__ = "role"

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (CheckConstraint("email = lower(btrim(email))", name="email_normalized"),)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("role.id", ondelete="RESTRICT"), nullable=False, server_default=text("1")
    )
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

    role: Mapped[Role] = relationship(lazy="joined")
    preferred_currency: Mapped[Currency] = relationship(lazy="joined")


Index("uq_app_user_email", func.lower(AppUser.email), unique=True)
