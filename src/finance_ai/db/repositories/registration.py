from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_ai.auth.repositories import CurrencyRecord, UserRecord
from finance_ai.core.errors import ServiceUnavailableError
from finance_ai.db.models.catalogs import Category, IncomeSource, PaymentMethod
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.seeds import RegistrationCatalogTemplate


class SQLAlchemyCurrencyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def require_active_by_code(self, code: str) -> CurrencyRecord:
        currency = self.session.scalar(
            select(Currency).where(Currency.code == code, Currency.is_active.is_(True))
        )
        if currency is None:
            raise ServiceUnavailableError
        return CurrencyRecord(id=currency.id, code=currency.code)


class SQLAlchemyUserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def exists_by_email(self, normalized_email: str) -> bool:
        return (
            self.session.scalar(
                select(AppUser.id).where(AppUser.email == normalized_email).limit(1)
            )
            is not None
        )

    def add(self, *, email: str, password_hash: str, preferred_currency_id: int) -> UserRecord:
        user = AppUser(
            email=email,
            password_hash=password_hash,
            preferred_currency_id=preferred_currency_id,
            is_active=True,
        )
        self.session.add(user)
        self.session.flush()
        return UserRecord(id=user.id, email=user.email)


class SQLAlchemyCatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_defaults(self, *, user_id: int, template: RegistrationCatalogTemplate) -> None:
        self.session.add_all(
            [Category(user_id=user_id, name=name, is_active=True) for name in template.categories]
        )
        self.session.add_all(
            [
                PaymentMethod(user_id=user_id, name=name, is_active=True)
                for name in template.payment_methods
            ]
        )
        self.session.add_all(
            [
                IncomeSource(user_id=user_id, name=name, is_active=True)
                for name in template.income_sources
            ]
        )
