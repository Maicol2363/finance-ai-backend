from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_ai.db.models.catalogs import Incomesource, PaymentMethod
from finance_ai.db.models.identity import Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.session import get_session_factory


@dataclass(frozen=True)
class RegistrationCatalogTemplate:
    version: str
    categories: tuple[str, ...]
    income_sources: tuple[str, ...]


REGISTRATION_CATALOG_TEMPLATE = RegistrationCatalogTemplate(
    version="2026-09-v2",
    categories=("Needs", "Wants", "Other"),
    income_sources=("Salary", "Other"),
)

CURRENCY_SEEDS = (
    {"code": "COP", "name": "Colombian Peso", "decimals": 2},
    {"code": "USD", "name": "US Dollar", "decimals": 2},
)

ROLE_SEEDS = ("user", "admin")

PAYMENT_METHOD_SEEDS = ("Cash", "Credit Card", "Debit Card", "Bank Transfer")

INCOME_SOURCE_SEEDS = ("Salary", "Freelance", "Investment", "Other")


def seed_shared_data(session: Session) -> None:
    existing_currencies = set(session.scalars(select(Currency.code)).all())
    for item in CURRENCY_SEEDS:
        if item["code"] not in existing_currencies:
            session.add(Currency(**item, is_active=True))

    existing_roles = set(session.scalars(select(Role.name)).all())
    for role_name in ROLE_SEEDS:
        if role_name not in existing_roles:
            session.add(Role(name=role_name))

    existing_payment_methods = set(session.scalars(select(PaymentMethod.name)).all())
    for pm_name in PAYMENT_METHOD_SEEDS:
        if pm_name not in existing_payment_methods:
            session.add(PaymentMethod(name=pm_name))

    existing_income_sources = set(session.scalars(select(Incomesource.name)).all())
    for inc_name in INCOME_SOURCE_SEEDS:
        if inc_name not in existing_income_sources:
            session.add(Incomesource(name=inc_name))

    session.commit()


def main() -> None:
    with get_session_factory()() as session:
        seed_shared_data(session)


if __name__ == "__main__":
    main()
