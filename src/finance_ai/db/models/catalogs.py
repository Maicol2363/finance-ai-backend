from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
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


class Category(Base):
    __tablename__ = "category"
    __table_args__ = (
        CheckConstraint("name = btrim(name) AND char_length(name) > 0", name="name_trimmed"),
        CheckConstraint(
            "color IN ('blue', 'green', 'orange', 'purple', 'pink', "
            "'red', 'teal', 'yellow', 'gray')",
            name="color_supported",
        ),
        CheckConstraint(
            "icon IN ('essentials', 'wants', 'food', 'transport', 'home', 'health', "
            "'education', 'entertainment', 'shopping', 'bills', 'travel', 'gift', 'other')",
            name="icon_supported",
        ),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'blue'"))
    icon: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'other'"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class IncomeSource(OwnedCatalogMixin, Base):
    __tablename__ = "income_source"


Index(
    "uq_category_user_name",
    Category.user_id,
    func.lower(Category.name),
    unique=True,
)
Index("ix_category_user_sort", Category.user_id, Category.sort_order, Category.id)
Index(
    "uq_income_source_user_name",
    IncomeSource.user_id,
    func.lower(IncomeSource.name),
    unique=True,
)
