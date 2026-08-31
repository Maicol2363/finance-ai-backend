from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from finance_ai.db.base import Base


class OwnedCatalogMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Category(OwnedCatalogMixin, Base):
    __tablename__ = "category"


class PaymentMethod(OwnedCatalogMixin, Base):
    __tablename__ = "payment_method"


class IncomeSource(OwnedCatalogMixin, Base):
    __tablename__ = "income_source"


Index(
    "uq_category_active_name",
    Category.user_id,
    func.lower(Category.name),
    unique=True,
    postgresql_where=Category.is_active.is_(True),
)
Index(
    "uq_payment_method_active_name",
    PaymentMethod.user_id,
    func.lower(PaymentMethod.name),
    unique=True,
    postgresql_where=PaymentMethod.is_active.is_(True),
)
Index(
    "uq_income_source_active_name",
    IncomeSource.user_id,
    func.lower(IncomeSource.name),
    unique=True,
    postgresql_where=IncomeSource.is_active.is_(True),
)
