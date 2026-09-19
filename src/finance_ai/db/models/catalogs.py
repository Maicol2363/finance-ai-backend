from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK


class OwnedCatalogMixin:
    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
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

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
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

    subcategories: Mapped[list[Subcategory]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class Subcategory(Base):
    __tablename__ = "subcategory"

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    category: Mapped[Category] = relationship(back_populates="subcategories")


class PaymentMethod(Base):
    __tablename__ = "paymentmethod"

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)


class IncomeSource(OwnedCatalogMixin, Base):
    __tablename__ = "income_source"


class Incomesource(Base):
    __tablename__ = "incomesource"

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)


class Store(Base):
    __tablename__ = "store"
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_name", name="uq_store_user_normalized_name"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


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
