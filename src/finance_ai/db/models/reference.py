from sqlalchemy import BigInteger, Boolean, CheckConstraint, Identity, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from finance_ai.db.base import Base


class Currency(Base):
    __tablename__ = "currency"
    __table_args__ = (CheckConstraint("decimals >= 0", name="decimals_non_negative"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
