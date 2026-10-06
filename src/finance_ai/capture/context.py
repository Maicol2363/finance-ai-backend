"""The caller's own catalogs, used to ground what the model understood."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    id: int
    name: str


@dataclass(frozen=True)
class AccountEntry:
    id: int
    name: str
    currency_code: str


@dataclass(frozen=True)
class CaptureContext:
    """Active accounts and catalogs of one user: the only values a proposal may reference."""

    accounts: tuple[AccountEntry, ...]
    categories: tuple[CatalogEntry, ...]
    income_sources: tuple[CatalogEntry, ...]

    def account(self, account_id: int | None) -> AccountEntry | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def category(self, category_id: int | None) -> CatalogEntry | None:
        return next((c for c in self.categories if c.id == category_id), None)

    def income_source(self, source_id: int | None) -> CatalogEntry | None:
        return next((s for s in self.income_sources if s.id == source_id), None)
