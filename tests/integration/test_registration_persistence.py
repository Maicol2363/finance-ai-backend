import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Literal, Self

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.repositories import CatalogRepository
from finance_ai.auth.schemas import RegisterCommand
from finance_ai.auth.service import RegistrationService
from finance_ai.auth.session_policy import SessionPolicy, SystemClock
from finance_ai.auth.session_schemas import IssuedSession, LoginCommand
from finance_ai.auth.session_service import SessionService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    AuthenticationRequiredError,
    EmailAlreadyRegisteredError,
    SessionInvalidError,
)
from finance_ai.db.models.catalogs import Category, IncomeSource, PaymentMethod
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.db.seeds import (
    REGISTRATION_CATALOG_TEMPLATE,
    RegistrationCatalogTemplate,
    seed_shared_data,
)
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.db.session_uow import SQLAlchemySessionUnitOfWork
from finance_ai.db.uow import SQLAlchemyRegistrationUnitOfWork

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


def require_safe_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    database_name = make_url(TEST_DATABASE_URL).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    database_url = require_safe_test_database_url()
    os.environ["FINANCE_AI_DATABASE_URL"] = database_url
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_registration_data(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE TABLE income_source, payment_method, category, app_user "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()


def registration_service(
    session_factory: sessionmaker[Session] | None = None,
) -> RegistrationService:
    factory = session_factory or get_session_factory()
    return RegistrationService(
        policy=RegistrationPolicy(),
        unsafe_passwords=UnsafePasswordProvider(get_settings().resolved_unsafe_passwords_path),
        password_hasher=PasswordHasher(memory_kib=19_456, time_cost=2, parallelism=1),
        uow_factory=lambda: SQLAlchemyRegistrationUnitOfWork(factory),
    )


def register(service: RegistrationService, email: str = "person@example.com") -> int:
    result = service.register(
        RegisterCommand(
            email=email,
            password="correct horse battery staple",
            password_confirmation="correct horse battery staple",
        )
    )
    return result.user_id


def test_migration_and_shared_seed_are_repeatable() -> None:
    with get_session_factory()() as session:
        currencies = session.scalars(select(Currency).order_by(Currency.code)).all()

    assert [(item.code, item.name, item.decimals) for item in currencies] == [
        ("COP", "Colombian Peso", 2),
        ("USD", "US Dollar", 2),
    ]


def test_registration_persists_one_owner_and_exact_catalogs() -> None:
    user_id = register(registration_service(), " Person@Example.COM ")

    with get_session_factory()() as session:
        user = session.get(AppUser, user_id)
        categories = session.scalars(
            select(Category).where(Category.user_id == user_id).order_by(Category.id)
        ).all()
        payment_methods = session.scalars(
            select(PaymentMethod).where(PaymentMethod.user_id == user_id).order_by(PaymentMethod.id)
        ).all()
        income_sources = session.scalars(
            select(IncomeSource).where(IncomeSource.user_id == user_id).order_by(IncomeSource.id)
        ).all()

    assert user is not None
    assert user.email == "person@example.com"
    assert user.preferred_currency.code == "COP"
    assert user.password_hash.startswith("$argon2id$")
    assert [item.name for item in categories] == list(REGISTRATION_CATALOG_TEMPLATE.categories)
    assert [item.name for item in payment_methods] == list(
        REGISTRATION_CATALOG_TEMPLATE.payment_methods
    )
    assert [item.name for item in income_sources] == list(
        REGISTRATION_CATALOG_TEMPLATE.income_sources
    )
    assert {item.user_id for item in categories + payment_methods + income_sources} == {user_id}


def test_catalog_failure_rolls_back_user_and_all_staged_rows() -> None:
    class FailingCatalogRepository:
        def __init__(self, delegate: CatalogRepository) -> None:
            self.delegate = delegate

        def add_defaults(self, *, user_id: int, template: RegistrationCatalogTemplate) -> None:
            self.delegate.add_defaults(user_id=user_id, template=template)
            raise RuntimeError("injected catalog failure")

    class FailingUnitOfWork(SQLAlchemyRegistrationUnitOfWork):
        def __enter__(self) -> Self:
            super().__enter__()
            self.catalogs = FailingCatalogRepository(self.catalogs)
            return self

    factory = get_session_factory()
    service = registration_service(factory)
    service.uow_factory = lambda: FailingUnitOfWork(factory)

    with pytest.raises(RuntimeError, match="injected catalog failure"):
        register(service)

    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(AppUser)) == 0
        assert session.scalar(select(func.count()).select_from(Category)) == 0
        assert session.scalar(select(func.count()).select_from(PaymentMethod)) == 0
        assert session.scalar(select(func.count()).select_from(IncomeSource)) == 0


def test_concurrent_normalized_duplicate_creates_exactly_one_user() -> None:
    def attempt(email: str) -> str:
        try:
            register(registration_service(), email)
        except EmailAlreadyRegisteredError:
            return "duplicate"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["Race@Example.com", "  race@example.COM  "]))

    assert sorted(results) == ["created", "duplicate"]
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(AppUser)) == 1
        assert session.scalar(select(func.count()).select_from(Category)) == 3
        assert session.scalar(select(func.count()).select_from(PaymentMethod)) == 3
        assert session.scalar(select(func.count()).select_from(IncomeSource)) == 2


def test_current_schema_adds_sessions_without_creating_later_financial_or_ai_tables() -> None:
    with get_session_factory()() as session:
        names = {
            name
            for name in (
                "account",
                "subcategory",
                "user_session",
                "renewable_credential",
                "ai_interaction",
            )
            if session.scalar(text("SELECT to_regclass(:name)"), {"name": f"public.{name}"})
            is not None
        }

    assert names == {"user_session", "renewable_credential"}


def test_session_check_constraint_names_match_model_metadata() -> None:
    with get_session_factory()() as session:
        constraint_names = set(
            session.scalars(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid IN (
                        'user_session'::regclass,
                        'renewable_credential'::regclass
                    )
                    AND contype = 'c'
                    """
                )
            )
        )

    assert constraint_names == {
        "ck_renewable_credential_renewable_expiry_after_issue",
        "ck_renewable_credential_renewable_hash_length",
        "ck_renewable_credential_replacement_requires_consumption",
        "ck_user_session_access_hash_length",
        "ck_user_session_revocation_pair",
        "ck_user_session_session_expiry_order",
    }


def secure_session_service() -> SessionService:
    settings = get_settings()
    return SessionService(
        password_hasher=PasswordHasher(memory_kib=19_456, time_cost=2, parallelism=1),
        credential_codec=CredentialCodec(),
        policy=SessionPolicy(
            access_lifetime=timedelta(minutes=settings.access_lifetime_minutes),
            renewable_idle_lifetime=timedelta(days=settings.renewable_idle_lifetime_days),
            absolute_lifetime=timedelta(days=settings.session_absolute_lifetime_days),
            max_active_sessions=settings.max_active_sessions_per_user,
            terminal_retention=timedelta(days=settings.terminal_session_retention_days),
        ),
        clock=SystemClock(),
        uow_factory=lambda: SQLAlchemySessionUnitOfWork(get_session_factory()),
    )


def login_session(
    service: SessionService,
    *,
    platform: Literal["android", "windows"] = "windows",
) -> IssuedSession:
    return service.login(
        LoginCommand(
            email="person@example.com",
            password="correct horse battery staple",
            platform=platform,
        )
    )


def test_login_access_refresh_replay_and_logout_are_hash_only_and_fail_closed() -> None:
    register(registration_service())
    service = secure_session_service()
    initial = login_session(service)

    with get_session_factory()() as session:
        stored_session = session.scalar(select(UserSession))
        stored_renewable = session.scalar(select(RenewableCredential))
        assert stored_session is not None and stored_renewable is not None
        assert len(stored_session.access_token_hash) == 64
        assert len(stored_renewable.token_hash) == 64
        assert initial.access_credential not in stored_session.access_token_hash
        assert initial.renewable_credential not in stored_renewable.token_hash

    assert service.current_user(initial.access_credential).email == "person@example.com"
    rotated = service.refresh(initial.renewable_credential)
    with pytest.raises(AuthenticationRequiredError):
        service.current_user(initial.access_credential)
    assert service.current_user(rotated.access_credential).email == "person@example.com"

    with pytest.raises(SessionInvalidError):
        service.refresh(initial.renewable_credential)
    with pytest.raises(AuthenticationRequiredError):
        service.current_user(rotated.access_credential)

    service.logout(rotated.renewable_credential)
    service.logout(rotated.renewable_credential)


def test_concurrent_same_credential_refresh_has_one_success_then_revokes_replay() -> None:
    register(registration_service())
    service = secure_session_service()
    initial = login_session(service)

    def attempt_refresh(_: int) -> str:
        try:
            service.refresh(initial.renewable_credential)
        except SessionInvalidError:
            return "invalid"
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt_refresh, range(2)))

    assert sorted(outcomes) == ["invalid", "success"]
    with get_session_factory()() as session:
        stored_session = session.scalar(select(UserSession))
        assert stored_session is not None
        assert stored_session.revocation_reason == "replay"


def test_concurrent_logins_leave_at_most_ten_active_sessions() -> None:
    register(registration_service())

    def attempt_login(index: int) -> str:
        service = secure_session_service()
        return login_session(
            service, platform="android" if index % 2 else "windows"
        ).access_credential

    with ThreadPoolExecutor(max_workers=4) as executor:
        credentials = list(executor.map(attempt_login, range(12)))

    assert len(set(credentials)) == 12
    with get_session_factory()() as session:
        active_count = session.scalar(
            select(func.count()).select_from(UserSession).where(UserSession.revoked_at.is_(None))
        )
        limited_count = session.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.revocation_reason == "session_limit")
        )
    assert active_count == 10
    assert limited_count == 2
