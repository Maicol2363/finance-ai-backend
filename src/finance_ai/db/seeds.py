from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_ai.db.models.reference import Currency
from finance_ai.db.session import get_session_factory


@dataclass(frozen=True)
class RegistrationCatalogTemplate:
    version: str
    categories: tuple[str, ...]
    payment_methods: tuple[str, ...]
    income_sources: tuple[str, ...]


REGISTRATION_CATALOG_TEMPLATE = RegistrationCatalogTemplate(
    version="2026-08-v1",
    categories=("Needs", "Wants", "Other"),
    payment_methods=("Cash", "Card", "Transfer"),
    income_sources=("Salary", "Other"),
)

CURRENCY_SEEDS = (
    {"code": "COP", "name": "Colombian Peso", "decimals": 2},
    {"code": "USD", "name": "US Dollar", "decimals": 2},
)


def seed_shared_data(session: Session) -> None:
    existing = set(session.scalars(select(Currency.code)).all())
    for item in CURRENCY_SEEDS:
        if item["code"] not in existing:
            session.add(Currency(**item, is_active=True))
    session.commit()


def main() -> None:
    with get_session_factory()() as session:
        seed_shared_data(session)


if __name__ == "__main__":
    main()
