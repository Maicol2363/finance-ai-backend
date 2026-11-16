"""ADM-01 lookup rules and ADM-02 service behaviour (routine behaviour is proven on PostgreSQL)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.admin.accounts_service import AdminAccountService, normalize_lookup
from finance_ai.admin.repository import AdminAccountRepository
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.errors import (
    AccountStateConflictError,
    AdminAccountNotFoundError,
    AdminRequestInvalidError,
    AdminSelfActionError,
    AuditUnavailableError,
    LastActiveAdminError,
    StaffAccessDeniedError,
)
from finance_ai.db.base import Base
from finance_ai.db.models.admin import AccountSuspension
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency

ADMIN_ID, CUSTOMER_ID, SUSPENDED_ID = 1, 2, 3
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class ScriptedRepository(AdminAccountRepository):
    """Replays a scripted routine outcome; it mimics P1's effect only for ``ok``."""

    outcome = "ok"

    def set_suspension(
        self, *, actor_id: int, target_id: int, suspend: bool, reason: str | None
    ) -> str:
        if self.outcome == "ok":
            user = self._session.get(AppUser, target_id)
            assert user is not None
            user.is_active = not suspend
            if suspend:
                self._session.add(
                    AccountSuspension(
                        user_id=target_id,
                        suspended_by_user_id=actor_id,
                        reason=reason or "",
                        suspended_at=NOW,
                    )
                )
            self._session.flush()
        return self.outcome


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://")
    event.listen(
        engine,
        "connect",
        lambda connection, _: (
            connection.create_function(
                "btrim", 1, lambda value: value.strip() if value is not None else None
            ),
            connection.create_function("char_length", 1, len),
        ),
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            Currency.__table__,
            AppUser.__table__,
            AccountSuspension.__table__,
            SecurityAuditEvent.__table__,
        ],  # type: ignore[arg-type]
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with session_factory() as session, session.begin():
        session.add_all(
            [
                Role(id=1, name="user"),
                Role(id=2, name="admin"),
                Currency(id=1, code="COP", name="Colombian peso", decimals=2, is_active=True),
            ]
        )
        session.flush()
        for user_id, email, role_id, active in (
            (ADMIN_ID, "admin@example.test", 2, True),
            (CUSTOMER_ID, "customer@example.test", 1, True),
            (SUSPENDED_ID, "off@example.test", 1, False),
        ):
            session.add(
                AppUser(
                    id=user_id,
                    email=email,
                    password_hash="never-returned",
                    role_id=role_id,
                    preferred_currency_id=1,
                    is_active=active,
                )
            )
        session.flush()
        session.add(
            AccountSuspension(
                user_id=SUSPENDED_ID,
                suspended_by_user_id=ADMIN_ID,
                reason="chargeback abuse",
                suspended_at=NOW,
            )
        )
    yield session_factory
    engine.dispose()


def service(
    factory: sessionmaker[Session],
    outcome: str = "ok",
    writer: SecurityAuditWriter | None = None,
) -> AdminAccountService:
    repository = type("Scripted", (ScriptedRepository,), {"outcome": outcome})
    return AdminAccountService(
        factory, writer or SecurityAuditWriter(), repository_factory=repository
    )


def audit_rows(factory: sessionmaker[Session]) -> list[SecurityAuditEvent]:
    with factory() as session:
        return list(session.scalars(select(SecurityAuditEvent)))


# --- ADM-01 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "account_id"),
    [(None, None), ("a@example.test", 1)],
)
def test_lookup_requires_exactly_one_key(email: str | None, account_id: int | None) -> None:
    with pytest.raises(AdminRequestInvalidError):
        normalize_lookup(email, account_id)


@pytest.mark.parametrize(
    "email", ["", "no-at-sign", "two@@example.test", "spaced out@example.test", "a@b\n.test", "@x"]
)
def test_lookup_rejects_malformed_emails(email: str) -> None:
    with pytest.raises(AdminRequestInvalidError):
        normalize_lookup(email, None)


@pytest.mark.parametrize("account_id", [0, -1, 2**63])
def test_lookup_rejects_out_of_range_ids(account_id: int) -> None:
    with pytest.raises(AdminRequestInvalidError):
        normalize_lookup(None, account_id)


def test_lookup_normalizes_email_and_never_matches_partial_values(
    factory: sessionmaker[Session],
) -> None:
    svc = service(factory)

    found = svc.lookup(email="  Customer@Example.TEST ", account_id=None)

    assert found.id == CUSTOMER_ID
    with pytest.raises(AdminAccountNotFoundError):
        svc.lookup(email="customer@example", account_id=None)
    with pytest.raises(AdminAccountNotFoundError):
        svc.lookup(email="%@example.test", account_id=None)


def test_lookup_projection_is_an_allowlist_without_credentials_or_financial_data(
    factory: sessionmaker[Session],
) -> None:
    account = service(factory).lookup(email=None, account_id=CUSTOMER_ID)

    assert set(account.model_dump()) == {
        "id",
        "email",
        "role",
        "status",
        "registered_at",
        "account_type",
        "suspension",
    }
    assert account.account_type == "login_account"
    assert account.status == "active" and account.suspension is None
    assert "never-returned" not in account.model_dump_json()


def test_suspended_account_exposes_its_open_suspension_to_staff(
    factory: sessionmaker[Session],
) -> None:
    account = service(factory).detail(SUSPENDED_ID)

    assert account.status == "suspended"
    assert account.suspension is not None
    assert account.suspension.reason == "chargeback abuse"
    assert account.suspension.suspended_by_user_id == ADMIN_ID


def test_unknown_account_is_not_found(factory: sessionmaker[Session]) -> None:
    with pytest.raises(AdminAccountNotFoundError):
        service(factory).detail(999)


# --- ADM-02 -----------------------------------------------------------------------------------


def test_successful_suspension_returns_the_new_state_and_records_one_event(
    factory: sessionmaker[Session],
) -> None:
    account = service(factory).suspend(
        actor_id=ADMIN_ID, target_id=CUSTOMER_ID, reason="fraud review", correlation_id="corr-1"
    )

    assert account.status == "suspended"
    assert account.suspension is not None and account.suspension.reason == "fraud review"
    (event_row,) = audit_rows(factory)
    assert event_row.event_type == "account.suspended" and event_row.result == "success"
    assert event_row.actor_id == str(ADMIN_ID) and event_row.target_resource_id == str(CUSTOMER_ID)
    assert event_row.target_resource_type == "account"
    assert event_row.correlation_id == "corr-1"
    assert event_row.context == {"reason": "administrator_action"}
    assert "fraud review" not in str(event_row.context)


def test_successful_reactivation_records_its_own_event(factory: sessionmaker[Session]) -> None:
    account = service(factory).reactivate(
        actor_id=ADMIN_ID, target_id=SUSPENDED_ID, correlation_id="corr-2"
    )

    assert account.status == "active"
    (event_row,) = audit_rows(factory)
    assert event_row.event_type == "account.reactivated" and event_row.result == "success"


@pytest.mark.parametrize(
    ("outcome", "error"),
    [("self_target", AdminSelfActionError), ("last_admin", LastActiveAdminError)],
)
def test_protected_denials_are_audited_as_denied_and_raise(
    factory: sessionmaker[Session], outcome: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        service(factory, outcome).suspend(
            actor_id=ADMIN_ID, target_id=ADMIN_ID, reason="x", correlation_id="corr-3"
        )

    (event_row,) = audit_rows(factory)
    assert event_row.event_type == "account.suspended" and event_row.result == "denied"
    assert event_row.context == {"reason": outcome}


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("not_found", AdminAccountNotFoundError),
        ("already_in_state", AccountStateConflictError),
        ("invalid_reason", AdminRequestInvalidError),
        ("actor_not_authorized", StaffAccessDeniedError),
    ],
)
def test_other_outcomes_raise_without_audit_events(
    factory: sessionmaker[Session], outcome: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        service(factory, outcome).suspend(
            actor_id=ADMIN_ID, target_id=CUSTOMER_ID, reason="x", correlation_id="corr-4"
        )

    assert audit_rows(factory) == []


def test_state_change_rolls_back_when_the_audit_event_cannot_be_written(
    factory: sessionmaker[Session],
) -> None:
    class FailingWriter(SecurityAuditWriter):
        def append(self, session: Session, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("audit store unavailable")

    with pytest.raises(AuditUnavailableError):
        service(factory, "ok", FailingWriter()).suspend(
            actor_id=ADMIN_ID, target_id=CUSTOMER_ID, reason="fraud", correlation_id="corr-5"
        )

    with factory() as session:
        customer = session.get(AppUser, CUSTOMER_ID)
        assert customer is not None and customer.is_active is True
        assert (
            session.scalar(
                select(AccountSuspension).where(AccountSuspension.user_id == CUSTOMER_ID)
            )
            is None
        )
    assert audit_rows(factory) == []
