"""PostgreSQL proof of the account routines: triggers T1/T2 and procedure P1 (ADM-02/ADM-04)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from finance_ai.core.config import get_settings
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")

CALL = text(
    "CALL sp_admin_set_account_suspension("
    "CAST(:actor AS bigint), CAST(:target AS bigint), CAST(:suspend AS boolean), "
    "CAST(:reason AS text), CAST(NULL AS text))"
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


def reset_rows() -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE TABLE security_audit_event, account_suspension, app_user "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()


@pytest.fixture(autouse=True)
def clean_rows(migrated_database: None) -> Iterator[None]:
    reset_rows()
    yield
    # Leave no audit evidence behind: other modules rebuild the schema from scratch and the
    # audit table refuses to be dropped while it holds events.
    reset_rows()


def make_user(role: str, email: str, *, active: bool = True) -> int:
    with get_session_factory()() as session:
        user_id = session.execute(
            text(
                "INSERT INTO app_user (email, password_hash, role_id, preferred_currency_id, "
                "is_active) VALUES (:email, 'hash', (SELECT id FROM role WHERE name = :role), "
                "(SELECT id FROM currency WHERE code = 'COP'), :active) RETURNING id"
            ),
            {"email": email, "role": role, "active": active},
        ).scalar_one()
        session.commit()
        return int(user_id)


def make_session(user_id: int, token: str, *, platform: str = "staff_browser") -> int:
    with get_session_factory()() as session:
        session_id = session.execute(
            text(
                "INSERT INTO user_session (user_id, platform, access_token_hash, "
                "access_expires_at, idle_expires_at, absolute_expires_at, last_renewed_at) "
                "VALUES (:user_id, :platform, :token, now() + interval '10 minutes', "
                "now() + interval '20 minutes', now() + interval '4 hours', now()) RETURNING id"
            ),
            {"user_id": user_id, "platform": platform, "token": token.ljust(64, "0")},
        ).scalar_one()
        session.commit()
        return int(session_id)


def call_suspension(actor: int, target: int, suspend: bool, reason: str | None) -> str:
    with get_session_factory()() as session:
        result = session.execute(
            CALL, {"actor": actor, "target": target, "suspend": suspend, "reason": reason}
        ).scalar_one()
        session.commit()
        return str(result)


def scalar(sql: str, **params: object) -> object:
    with get_session_factory()() as session:
        return session.execute(text(sql), params).scalar_one()


# --- T1: last active administrator guard ------------------------------------------------------


def test_t1_blocks_deactivating_the_only_active_administrator_by_direct_sql() -> None:
    admin = make_user("admin", "only@example.test")

    with get_session_factory()() as session:
        with pytest.raises(IntegrityError, match="last active administrator"):
            session.execute(
                text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": admin}
            )
        session.rollback()

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=admin) is True


def test_t1_blocks_demoting_or_deleting_the_only_active_administrator() -> None:
    admin = make_user("admin", "only@example.test")

    for statement in (
        "UPDATE app_user SET role_id = (SELECT id FROM role WHERE name = 'user') WHERE id = :id",
        "DELETE FROM app_user WHERE id = :id",
    ):
        with get_session_factory()() as session:
            with pytest.raises(IntegrityError, match="last active administrator"):
                session.execute(text(statement), {"id": admin})
            session.rollback()
    assert scalar("SELECT count(*) FROM app_user") == 1


def test_t1_allows_removing_an_administrator_while_another_remains_active() -> None:
    first = make_user("admin", "one@example.test")
    make_user("admin", "two@example.test")

    with get_session_factory()() as session:
        session.execute(text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": first})
        session.commit()

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=first) is False


def test_t1_ignores_other_roles_inactive_admins_and_unrelated_columns() -> None:
    admin = make_user("admin", "only@example.test")
    customer = make_user("user", "customer@example.test")
    inactive_admin = make_user("admin", "former@example.test", active=False)

    with get_session_factory()() as session:
        session.execute(
            text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": customer}
        )
        session.execute(
            text(
                "UPDATE app_user SET role_id = (SELECT id FROM role WHERE name = 'user') "
                "WHERE id = :id"
            ),
            {"id": inactive_admin},
        )
        session.execute(
            text("UPDATE app_user SET email = 'renamed@example.test' WHERE id = :id"), {"id": admin}
        )
        session.commit()

    assert scalar("SELECT email FROM app_user WHERE id = :id", id=admin) == "renamed@example.test"


def test_t1_serializes_competing_removals_so_one_administrator_always_survives() -> None:
    first = make_user("admin", "one@example.test")
    second = make_user("admin", "two@example.test")
    holding = threading.Event()
    release = threading.Event()
    outcomes: dict[str, str] = {}

    def remove(name: str, target: int, hold: bool) -> None:
        with get_session_factory()() as session:
            try:
                session.execute(
                    text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": target}
                )
                if hold:
                    holding.set()
                    release.wait(timeout=10)
                session.commit()
                outcomes[name] = "removed"
            except IntegrityError as error:
                session.rollback()
                outcomes[name] = "blocked" if "last active administrator" in str(error) else "error"

    winner = threading.Thread(target=remove, args=("first", first, True))
    loser = threading.Thread(target=remove, args=("second", second, False))
    winner.start()
    assert holding.wait(timeout=10)
    loser.start()
    time.sleep(0.5)  # the second removal is now waiting on the invariant lock
    release.set()
    winner.join(timeout=15)
    loser.join(timeout=15)

    assert sorted(outcomes.values()) == ["blocked", "removed"]
    assert scalar("SELECT count(*) FROM app_user WHERE is_active") == 1


def test_t1_fails_closed_outside_read_committed_isolation() -> None:
    admin = make_user("admin", "one@example.test")
    make_user("admin", "two@example.test")

    with get_session_factory()() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        with pytest.raises(DBAPIError, match="READ COMMITTED"):
            session.execute(
                text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": admin}
            )
        session.rollback()


# --- T2: sessions are revoked when authority changes -----------------------------------------


def test_t2_revokes_every_live_session_when_an_account_is_suspended() -> None:
    make_user("admin", "admin@example.test")
    target = make_user("support_agent", "agent@example.test")
    other = make_user("support_agent", "other@example.test")
    mine = make_session(target, "a")
    second = make_session(target, "b", platform="android")
    untouched = make_session(other, "c")

    with get_session_factory()() as session:
        session.execute(
            text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": target}
        )
        session.commit()

    for session_id in (mine, second):
        assert (
            scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
            == "suspended"
        )
    assert (
        scalar("SELECT revoked_at IS NULL FROM user_session WHERE id = :id", id=untouched) is True
    )


def test_t2_revokes_sessions_on_role_change_and_never_restores_on_reactivation() -> None:
    make_user("admin", "admin@example.test")
    agent = make_user("support_agent", "agent@example.test")
    session_id = make_session(agent, "a")

    with get_session_factory()() as session:
        session.execute(
            text(
                "UPDATE app_user SET role_id = "
                "(SELECT id FROM role WHERE name = 'security_auditor') WHERE id = :id"
            ),
            {"id": agent},
        )
        session.commit()
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "role_changed"
    )

    with get_session_factory()() as session:
        session.execute(text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": agent})
        session.execute(text("UPDATE app_user SET is_active = true WHERE id = :id"), {"id": agent})
        session.commit()
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "role_changed"
    )


def test_t2_does_not_touch_sessions_for_unrelated_updates() -> None:
    make_user("admin", "admin@example.test")
    agent = make_user("support_agent", "agent@example.test")
    session_id = make_session(agent, "a")

    with get_session_factory()() as session:
        session.execute(
            text("UPDATE app_user SET email = 'new@example.test' WHERE id = :id"), {"id": agent}
        )
        session.commit()

    assert (
        scalar("SELECT revoked_at IS NULL FROM user_session WHERE id = :id", id=session_id) is True
    )


# --- P1: sp_admin_set_account_suspension -------------------------------------------------------


def test_p1_suspends_with_trimmed_reason_history_and_session_revocation() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    session_id = make_session(customer, "a", platform="android")

    assert call_suspension(admin, customer, True, "  fraud review  ") == "ok"

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=customer) is False
    assert (
        scalar("SELECT reason FROM account_suspension WHERE user_id = :id", id=customer)
        == "fraud review"
    )
    assert (
        scalar(
            "SELECT suspended_by_user_id FROM account_suspension WHERE user_id = :id", id=customer
        )
        == admin
    )
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "suspended"
    )


def test_p1_reactivation_closes_the_history_row_and_keeps_old_sessions_revoked() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    session_id = make_session(customer, "a")
    call_suspension(admin, customer, True, "review")

    assert call_suspension(admin, customer, False, None) == "ok"

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=customer) is True
    assert (
        scalar(
            "SELECT reactivated_by_user_id FROM account_suspension WHERE user_id = :id", id=customer
        )
        == admin
    )
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "suspended"
    )
    assert call_suspension(admin, customer, True, "second review") == "ok"
    assert scalar("SELECT count(*) FROM account_suspension WHERE user_id = :id", id=customer) == 2


@pytest.mark.parametrize("reason", [None, "", "   ", "x" * 501])
def test_p1_rejects_blank_or_oversized_reasons_without_changing_state(reason: str | None) -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")

    assert call_suspension(admin, customer, True, reason) == "invalid_reason"

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=customer) is True
    assert scalar("SELECT count(*) FROM account_suspension") == 0


def test_p1_rejects_self_suspension_unknown_targets_and_repeated_state() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")

    assert call_suspension(admin, admin, True, "no") == "self_target"
    assert call_suspension(admin, 987654, True, "no") == "not_found"
    assert call_suspension(admin, customer, False, None) == "already_in_state"
    call_suspension(admin, customer, True, "once")
    assert call_suspension(admin, customer, True, "twice") == "already_in_state"
    assert scalar("SELECT count(*) FROM account_suspension") == 1


@pytest.mark.parametrize("actor_role", ["user", "support_agent", "security_auditor"])
def test_p1_refuses_actors_that_are_not_active_administrators(actor_role: str) -> None:
    make_user("admin", "admin@example.test")
    actor = make_user(actor_role, "actor@example.test")
    customer = make_user("user", "customer@example.test")

    assert call_suspension(actor, customer, True, "reason") == "actor_not_authorized"
    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=customer) is True


def test_p1_refuses_a_suspended_administrator_actor() -> None:
    first = make_user("admin", "one@example.test")
    second = make_user("admin", "two@example.test")
    customer = make_user("user", "customer@example.test")
    assert call_suspension(second, first, True, "rotation") == "ok"

    assert call_suspension(first, customer, True, "reason") == "actor_not_authorized"


def test_p1_can_suspend_another_administrator_but_never_the_last_one() -> None:
    first = make_user("admin", "one@example.test")
    second = make_user("admin", "two@example.test")

    assert call_suspension(first, second, True, "rotation") == "ok"
    # `second` is now suspended, so `first` is the last active administrator.
    third = make_user("admin", "three@example.test", active=False)
    assert call_suspension(first, third, True, "already off") == "already_in_state"
    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=first) is True


def test_p1_held_mutual_suspension_refuses_the_second_actor_as_no_longer_authorized() -> None:
    first = make_user("admin", "one@example.test")
    second = make_user("admin", "two@example.test")
    holding = threading.Event()
    release = threading.Event()
    results: dict[str, str] = {}

    def suspend(name: str, actor: int, target: int, hold: bool) -> None:
        with get_session_factory()() as session:
            result = session.execute(
                CALL, {"actor": actor, "target": target, "suspend": True, "reason": "mutual"}
            ).scalar_one()
            if hold:
                holding.set()
                release.wait(timeout=10)
            session.commit()
            results[name] = str(result)

    one = threading.Thread(target=suspend, args=("one", first, second, True))
    two = threading.Thread(target=suspend, args=("two", second, first, False))
    one.start()
    assert holding.wait(timeout=10)
    two.start()
    time.sleep(0.5)
    release.set()
    one.join(timeout=15)
    two.join(timeout=15)

    assert results["one"] == "ok"
    # `two` queued behind `one`'s locks; once `one` committed, its actor had been suspended.
    assert results["two"] == "actor_not_authorized"
    assert (
        scalar(
            "SELECT count(*) FROM app_user WHERE is_active AND role_id = "
            "(SELECT id FROM role WHERE name = 'admin')"
        )
        == 1
    )
    assert scalar("SELECT count(*) FROM account_suspension") == 1


ADMIN_COUNT = (
    "SELECT count(*) FROM app_user WHERE is_active AND role_id = "
    "(SELECT id FROM role WHERE name = 'admin')"
)


def race(calls: list[Callable[[], str]]) -> tuple[list[str], list[str]]:
    """Run the calls at the same instant; return (results, database errors)."""
    barrier = threading.Barrier(len(calls))
    results: list[str] = []
    errors: list[str] = []
    guard = threading.Lock()

    def run(call: Callable[[], str]) -> None:
        barrier.wait(timeout=10)
        try:
            outcome = call()
        except DBAPIError as error:
            with guard:
                errors.append(f"{type(error.orig).__name__}: {error.orig}")
            return
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def test_p1_simultaneous_mutual_suspension_never_deadlocks() -> None:
    """Two administrators suspend each other at the same instant, many times over.

    Row locks on the actor and the target are taken in ascending id order, so the pair can
    never wait on each other: exactly one suspension wins and the other finds that its actor
    is no longer an active administrator. No PostgreSQL deadlock may ever surface.
    """

    def one_round() -> None:
        reset_rows()
        first = make_user("admin", "one@example.test")
        second = make_user("admin", "two@example.test")
        results, errors = race(
            [
                lambda: call_suspension(first, second, True, "mutual"),
                lambda: call_suspension(second, first, True, "mutual"),
            ]
        )
        assert errors == []
        assert sorted(results) == ["actor_not_authorized", "ok"]
        assert scalar(ADMIN_COUNT) == 1
        assert scalar("SELECT count(*) FROM account_suspension") == 1

    for _ in range(30):
        one_round()


def test_p1_simultaneous_suspensions_of_one_target_from_two_administrators() -> None:
    def one_round() -> None:
        reset_rows()
        first = make_user("admin", "one@example.test")
        second = make_user("admin", "two@example.test")
        customer = make_user("user", "customer@example.test")
        results, errors = race(
            [
                lambda: call_suspension(first, customer, True, "same"),
                lambda: call_suspension(second, customer, True, "same"),
            ]
        )
        assert errors == []
        assert sorted(results) == ["already_in_state", "ok"]
        assert scalar("SELECT count(*) FROM account_suspension") == 1

    for _ in range(15):
        one_round()


def test_service_simultaneous_mutual_suspension_is_a_clean_refusal_not_a_failure() -> None:
    from finance_ai.core.errors import StaffAccessDeniedError

    def suspend(actor: int, target: int) -> str:
        try:
            real_service().suspend(
                actor_id=actor, target_id=target, reason="mutual", correlation_id=f"c-{actor}"
            )
        except StaffAccessDeniedError:
            return "denied"
        return "ok"

    def one_round() -> None:
        reset_rows()
        first = make_user("admin", "one@example.test")
        second = make_user("admin", "two@example.test")
        results, errors = race([lambda: suspend(first, second), lambda: suspend(second, first)])
        assert errors == []
        assert sorted(results) == ["denied", "ok"]
        assert scalar(ADMIN_COUNT) == 1

    for _ in range(15):
        one_round()


def test_open_suspension_is_unique_per_account() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    call_suspension(admin, customer, True, "first")

    with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO account_suspension (user_id, suspended_by_user_id, reason) "
                    "VALUES (:u, :a, 'duplicate open row')"
                ),
                {"u": customer, "a": admin},
            )
        session.rollback()


# --- AdminAccountService on PostgreSQL: state change and audit evidence are one transaction ----


def audit_events() -> list[tuple[str, str, str | None, str | None, dict[str, object]]]:
    with get_session_factory()() as session:
        rows = session.execute(
            text(
                "SELECT event_type, result, actor_id, target_resource_id, context "
                "FROM security_audit_event ORDER BY occurred_at, event_id"
            )
        )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def real_service(writer: object | None = None):  # type: ignore[no-untyped-def]
    from finance_ai.admin.accounts_service import AdminAccountService
    from finance_ai.audit.service import SecurityAuditWriter

    return AdminAccountService(get_session_factory(), writer or SecurityAuditWriter())  # type: ignore[arg-type]


def test_service_suspension_revokes_sessions_and_commits_one_audit_event() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    session_id = make_session(customer, "a", platform="android")

    account = real_service().suspend(
        actor_id=admin, target_id=customer, reason="  fraud review ", correlation_id="corr-1"
    )

    assert account.status == "suspended" and account.suspension is not None
    assert account.suspension.reason == "fraud review"
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "suspended"
    )
    assert audit_events() == [
        (
            "account.suspended",
            "success",
            str(admin),
            str(customer),
            {"reason": "administrator_action"},
        )
    ]


def test_service_self_suspension_is_denied_audited_and_changes_nothing() -> None:
    from finance_ai.core.errors import AdminSelfActionError

    admin = make_user("admin", "admin@example.test")
    make_user("admin", "second@example.test")

    with pytest.raises(AdminSelfActionError):
        real_service().suspend(
            actor_id=admin, target_id=admin, reason="oops", correlation_id="corr-2"
        )

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=admin) is True
    assert audit_events()[0][:2] == ("account.suspended", "denied")
    assert scalar("SELECT count(*) FROM account_suspension") == 0


def test_service_rolls_back_the_state_change_when_the_audit_event_fails() -> None:
    from finance_ai.audit.service import SecurityAuditWriter
    from finance_ai.core.errors import AuditUnavailableError

    class FailingWriter(SecurityAuditWriter):
        def append(self, session, **kwargs):  # type: ignore[no-untyped-def,override]
            raise RuntimeError("audit store unavailable")

    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    session_id = make_session(customer, "a")

    with pytest.raises(AuditUnavailableError):
        real_service(FailingWriter()).suspend(
            actor_id=admin, target_id=customer, reason="fraud", correlation_id="corr-3"
        )

    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=customer) is True
    assert scalar("SELECT count(*) FROM account_suspension") == 0
    assert (
        scalar("SELECT revoked_at IS NULL FROM user_session WHERE id = :id", id=session_id) is True
    )
    assert audit_events() == []


def test_service_reactivation_never_restores_old_sessions() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    session_id = make_session(customer, "a")
    service = real_service()
    service.suspend(actor_id=admin, target_id=customer, reason="review", correlation_id="c1")

    account = service.reactivate(actor_id=admin, target_id=customer, correlation_id="c2")

    assert account.status == "active" and account.suspension is None
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "suspended"
    )
    assert [event[0] for event in audit_events()] == ["account.suspended", "account.reactivated"]


def test_service_held_mutual_suspension_refuses_the_second_actor() -> None:
    from finance_ai.core.errors import StaffAccessDeniedError

    first = make_user("admin", "one@example.test")
    second = make_user("admin", "two@example.test")
    holding, release = threading.Event(), threading.Event()
    outcomes: dict[str, str] = {}

    def run(name: str, actor: int, target: int, hold: bool) -> None:
        try:
            if hold:
                # Hold the first transaction open through the same procedure the service uses.
                with get_session_factory()() as session:
                    session.execute(
                        CALL,
                        {"actor": actor, "target": target, "suspend": True, "reason": "mutual"},
                    )
                    holding.set()
                    release.wait(timeout=10)
                    session.commit()
                outcomes[name] = "ok"
            else:
                real_service().suspend(
                    actor_id=actor, target_id=target, reason="mutual", correlation_id="c-" + name
                )
                outcomes[name] = "ok"
        except StaffAccessDeniedError:
            outcomes[name] = "denied"

    one = threading.Thread(target=run, args=("one", first, second, True))
    two = threading.Thread(target=run, args=("two", second, first, False))
    one.start()
    assert holding.wait(timeout=10)
    two.start()
    time.sleep(0.5)
    release.set()
    one.join(timeout=15)
    two.join(timeout=15)

    assert outcomes == {"one": "ok", "two": "denied"}
    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=first) is True
    assert scalar("SELECT is_active FROM app_user WHERE id = :id", id=second) is False
    assert scalar("SELECT count(*) FROM account_suspension") == 1


# --- ADM-03 / ADM-04 on PostgreSQL: single use under concurrency, role change -----------------


def staff_services():  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from finance_ai.admin.staff_service import AdminStaffService
    from finance_ai.audit.service import SecurityAuditWriter
    from finance_ai.auth.credentials import CredentialCodec
    from finance_ai.auth.dependencies import get_password_hasher, get_unsafe_password_provider
    from finance_ai.auth.session_policy import SystemClock
    from finance_ai.staff_auth.activation import StaffActivationService

    codec, clock, audit = CredentialCodec(), SystemClock(), SecurityAuditWriter()
    factory = get_session_factory()
    staff = AdminStaffService(factory, audit, codec, clock, invitation_lifetime=timedelta(hours=48))
    activation = StaffActivationService(
        session_factory=factory,
        password_hasher=get_password_hasher(),
        unsafe_passwords=get_unsafe_password_provider(),
        credential_codec=codec,
        clock=clock,
        audit_writer=audit,
    )
    return staff, activation


STRONG_PASSWORD = "a-long-and-unusual-passphrase"


def test_concurrent_activation_consumes_a_single_use_token_exactly_once() -> None:
    from finance_ai.core.errors import StaffActivationInvalidError

    admin = make_user("admin", "admin@example.test")
    staff, activation = staff_services()
    token = staff.create_invitation(
        actor_id=admin, email="agent@example.com", role="support_agent", correlation_id="c"
    ).activation_token
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            activation.activate(
                token=token,
                password=STRONG_PASSWORD,
                password_confirmation=STRONG_PASSWORD,
                correlation_id=f"c-{index}",
            )
            result = "activated"
        except StaffActivationInvalidError:
            result = "rejected"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["activated", "rejected", "rejected", "rejected"]
    assert scalar("SELECT count(*) FROM app_user WHERE email = 'agent@example.com'") == 1
    assert (
        scalar("SELECT count(*) FROM security_audit_event WHERE event_type = 'staff.activated'")
        == 1
    )
    assert (
        scalar(
            "SELECT activated_user_id IS NOT NULL AND consumed_at IS NOT NULL FROM staff_invitation"
        )
        is True
    )


def test_concurrent_invitations_for_one_email_create_a_single_pending_invitation() -> None:
    from finance_ai.core.errors import StaffEmailUnavailableError

    admin = make_user("admin", "admin@example.test")
    staff, _activation = staff_services()
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            staff.create_invitation(
                actor_id=admin,
                email="dup@example.com",
                role="security_auditor",
                correlation_id=f"c-{index}",
            )
            result = "created"
        except StaffEmailUnavailableError:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["created", "refused", "refused", "refused"]
    assert scalar("SELECT count(*) FROM staff_invitation WHERE email = 'dup@example.com'") == 1


def test_activated_staff_role_change_revokes_sessions_and_audits_on_postgres() -> None:
    admin = make_user("admin", "admin@example.test")
    staff, activation = staff_services()
    token = staff.create_invitation(
        actor_id=admin, email="mover@example.com", role="support_agent", correlation_id="c1"
    ).activation_token
    activation.activate(
        token=token,
        password=STRONG_PASSWORD,
        password_confirmation=STRONG_PASSWORD,
        correlation_id="c2",
    )
    mover = int(scalar("SELECT id FROM app_user WHERE email = 'mover@example.com'"))  # type: ignore[arg-type]
    session_id = make_session(mover, "m")

    view = staff.change_role(
        actor_id=admin, staff_id=mover, new_role="security_auditor", correlation_id="c3"
    )

    assert view.role == "security_auditor"
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "role_changed"
    )
    last = audit_events()[-1]
    assert last[0] == "role.changed" and last[4] == {
        "previous_role": "support_agent",
        "new_role": "security_auditor",
    }
    assert (
        scalar(
            "SELECT count(*) FROM app_user WHERE id = :id AND role_id = "
            "(SELECT id FROM role WHERE name = 'security_auditor')",
            id=mover,
        )
        == 1
    )


def test_a_used_or_expired_invitation_cannot_be_reused_after_commit() -> None:
    from finance_ai.core.errors import StaffActivationInvalidError

    admin = make_user("admin", "admin@example.test")
    staff, activation = staff_services()
    token = staff.create_invitation(
        actor_id=admin, email="once@example.com", role="support_agent", correlation_id="c1"
    ).activation_token
    activation.activate(
        token=token,
        password=STRONG_PASSWORD,
        password_confirmation=STRONG_PASSWORD,
        correlation_id="c2",
    )

    with pytest.raises(StaffActivationInvalidError):
        activation.activate(
            token=token,
            password="another-long-passphrase",
            password_confirmation="another-long-passphrase",
            correlation_id="c3",
        )
    with get_session_factory()() as session:
        session.execute(
            text(
                "UPDATE staff_invitation SET consumed_at = NULL, activated_user_id = NULL, "
                "expires_at = created_at + interval '1 second' WHERE email = 'once@example.com'"
            )
        )
        session.commit()
    with pytest.raises(StaffActivationInvalidError):
        activation.activate(
            token=token,
            password=STRONG_PASSWORD,
            password_confirmation=STRONG_PASSWORD,
            correlation_id="c4",
        )


# --- ADM-05 on PostgreSQL: escalated tickets, claim, return (P2), customer visibility ---------


def ticket_services():  # type: ignore[no-untyped-def]
    from finance_ai.admin.tickets_service import AdminTicketService
    from finance_ai.audit.service import SecurityAuditWriter
    from finance_ai.auth.session_policy import SystemClock
    from finance_ai.support.service import SupportTicketService
    from finance_ai.support.workflow import TicketWorkflow

    factory = get_session_factory()
    return (
        AdminTicketService(factory, SecurityAuditWriter(), SystemClock()),
        TicketWorkflow(factory),
        SupportTicketService(factory, SecurityAuditWriter()),
    )


ESCALATION_REASON = "ESCALATION-REASON refund policy exception"
RETURN_REASON = "RETURN-REASON customer verified, continue"
RESOLUTION = "Account restored after identity check."


def staff_user(user_id: int, role: str):  # type: ignore[no-untyped-def]
    from finance_ai.staff_auth.schemas import StaffSessionUser

    return StaffSessionUser(id=user_id, email=f"u{user_id}@example.test", role=role)


def make_escalated_ticket(agent_id: int, customer_id: int) -> int:
    """Drive the real Support workflow: create, claim and escalate."""
    from finance_ai.support.schemas import AgentTicketCreate

    _admin_tickets, workflow, support = ticket_services()
    ticket = workflow.create(
        agent_id,
        AgentTicketCreate(
            subject="Cannot sign in",
            category="account",
            description="Customer cannot sign in after the reset",
            customer_identifier=str(customer_id),
        ),
        "corr-create",
        staff=True,
    )
    support.claim(staff_user(agent_id, "support_agent"), ticket.id, "corr-claim")
    workflow.transition(agent_id, ticket.id, ESCALATION_REASON, "corr-escalate", escalate=True)
    return ticket.id


def ticket_row(ticket_id: int) -> dict[str, object]:
    with get_session_factory()() as session:
        row = session.execute(
            text(
                "SELECT status, assigned_staff_user_id, admin_owner_user_id, "
                "originating_support_agent_id, resolution_summary FROM support_ticket "
                "WHERE id = :id"
            ),
            {"id": ticket_id},
        ).one()
        return dict(row._mapping)


def activity_types(ticket_id: int) -> list[str]:
    with get_session_factory()() as session:
        rows = session.execute(
            text(
                "SELECT event_type FROM support_ticket_activity WHERE ticket_id = :id ORDER BY id"
            ),
            {"id": ticket_id},
        )
        return [row[0] for row in rows]


def support_world() -> tuple[int, int, int, int]:
    admin = make_user("admin", "admin@example.test")
    agent = make_user("support_agent", "agent@example.test")
    customer = make_user("user", "customer@example.test")
    other_admin = make_user("admin", "admin2@example.test")
    return admin, agent, customer, other_admin


def test_adm05_full_lifecycle_return_to_the_active_originating_agent() -> None:
    admin, agent, customer, _other = support_world()
    tickets, workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)

    queue = tickets.list_escalated(admin_id=admin, scope="unclaimed", limit=50)
    assert [item.id for item in queue] == [ticket_id] and queue[0].owned_by_me is False

    case = tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c-claim")
    assert case.owned_by_me and case.escalation_reason == ESCALATION_REASON
    assert case.originating_agent.user_id == agent and case.originating_agent.can_receive_return
    assert case.customer.email == "customer@example.test"

    tickets.reply(
        admin_id=admin, ticket_id=ticket_id, body="We are checking.", correlation_id="c-r"
    )
    outcome = tickets.return_to_support(
        admin_id=admin, ticket_id=ticket_id, reason=RETURN_REASON, correlation_id="c-ret"
    )

    assert outcome.status == "in_progress" and outcome.assigned_staff_user_id == agent
    assert ticket_row(ticket_id) == {
        "status": "in_progress",
        "assigned_staff_user_id": agent,
        "admin_owner_user_id": None,
        "originating_support_agent_id": agent,
        "resolution_summary": None,
    }
    assert activity_types(ticket_id) == [
        "ticket_created",
        "ticket_claimed",
        "ticket_escalated",
        "ticket_admin_claimed",
        "message_sent",
        "ticket_returned",
    ]
    workflow.message(agent, ticket_id, "Back with Support.", "c-agent", staff=True)
    events = [event[:2] for event in audit_events()]
    assert ("support.ticket_returned", "success") in events
    assert ("ticket.escalated", "success") in events


@pytest.mark.parametrize("ineligible", ["suspended", "role_changed", "no_origin"])
def test_adm05_return_goes_to_the_open_queue_when_the_origin_is_not_eligible(
    ineligible: str,
) -> None:
    admin, agent, customer, _other = support_world()
    tickets, _workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c-claim")
    with get_session_factory()() as session:
        if ineligible == "suspended":
            session.execute(
                text("UPDATE app_user SET is_active = false WHERE id = :id"), {"id": agent}
            )
        elif ineligible == "role_changed":
            session.execute(
                text(
                    "UPDATE app_user SET role_id = "
                    "(SELECT id FROM role WHERE name = 'security_auditor') WHERE id = :id"
                ),
                {"id": agent},
            )
        else:
            session.execute(
                text(
                    "UPDATE support_ticket SET originating_support_agent_id = NULL WHERE id = :id"
                ),
                {"id": ticket_id},
            )
        session.commit()

    outcome = tickets.return_to_support(
        admin_id=admin, ticket_id=ticket_id, reason=RETURN_REASON, correlation_id="c-ret"
    )

    assert outcome.status == "open" and outcome.assigned_staff_user_id is None
    row = ticket_row(ticket_id)
    assert row["status"] == "open" and row["assigned_staff_user_id"] is None
    assert row["admin_owner_user_id"] is None


def test_adm05_only_one_administrator_can_claim_under_concurrency() -> None:
    from finance_ai.core.errors import EscalationClaimConflictError

    admin, agent, customer, other_admin = support_world()
    third = make_user("admin", "admin3@example.test")
    fourth = make_user("admin", "admin4@example.test")
    tickets, _workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    barrier = threading.Barrier(4)
    outcomes: dict[int, str] = {}
    lock = threading.Lock()

    def attempt(admin_id: int) -> None:
        barrier.wait(timeout=10)
        try:
            tickets.claim(admin_id=admin_id, ticket_id=ticket_id, correlation_id=f"c-{admin_id}")
            result = "claimed"
        except EscalationClaimConflictError:
            result = "conflict"
        with lock:
            outcomes[admin_id] = result

    threads = [
        threading.Thread(target=attempt, args=(a,)) for a in (admin, other_admin, third, fourth)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes.values()) == ["claimed", "conflict", "conflict", "conflict"]
    winner = next(admin_id for admin_id, result in outcomes.items() if result == "claimed")
    assert ticket_row(ticket_id)["admin_owner_user_id"] == winner
    assert activity_types(ticket_id).count("ticket_admin_claimed") == 1


def test_adm05_mutations_require_current_ownership_and_an_escalated_ticket() -> None:
    from finance_ai.core.errors import SupportTicketNotFoundError, SupportTicketStateConflictError

    admin, agent, customer, other_admin = support_world()
    tickets, _workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")
    before = ticket_row(ticket_id)

    with pytest.raises(SupportTicketStateConflictError):
        tickets.reply(admin_id=other_admin, ticket_id=ticket_id, body="hi", correlation_id="c")
    with pytest.raises(SupportTicketStateConflictError):
        tickets.resolve(admin_id=other_admin, ticket_id=ticket_id, summary="x", correlation_id="c")
    with pytest.raises(SupportTicketStateConflictError):
        tickets.return_to_support(
            admin_id=other_admin, ticket_id=ticket_id, reason="x", correlation_id="c"
        )
    with pytest.raises(SupportTicketNotFoundError):
        tickets.get_case(admin_id=admin, ticket_id=987654)

    assert ticket_row(ticket_id) == before


def test_adm05_unescalated_tickets_are_not_reachable_by_administrators() -> None:
    from finance_ai.core.errors import SupportTicketNotFoundError
    from finance_ai.support.schemas import AgentTicketCreate

    admin, agent, customer, _other = support_world()
    tickets, workflow, _support = ticket_services()
    ticket = workflow.create(
        agent,
        AgentTicketCreate(
            subject="Plain ticket",
            category="general",
            description="Not escalated",
            customer_identifier=str(customer),
        ),
        "c",
        staff=True,
    )

    with pytest.raises(SupportTicketNotFoundError):
        tickets.get_case(admin_id=admin, ticket_id=ticket.id)
    with pytest.raises(SupportTicketNotFoundError):
        tickets.claim(admin_id=admin, ticket_id=ticket.id, correlation_id="c")
    assert tickets.list_escalated(admin_id=admin, scope="all", limit=100) == []


def test_adm05_resolution_is_customer_visible_but_staff_reasons_never_reach_the_customer() -> None:
    admin, agent, customer, _other = support_world()
    tickets, workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")
    tickets.reply(admin_id=admin, ticket_id=ticket_id, body="Public reply", correlation_id="c")
    tickets.return_to_support(
        admin_id=admin, ticket_id=ticket_id, reason=RETURN_REASON, correlation_id="c"
    )
    workflow.transition(agent, ticket_id, ESCALATION_REASON, "c", escalate=True)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")

    tickets.resolve(admin_id=admin, ticket_id=ticket_id, summary=RESOLUTION, correlation_id="c")

    assert ticket_row(ticket_id)["status"] == "resolved"
    assert ticket_row(ticket_id)["admin_owner_user_id"] is None
    conversation = workflow.read(customer, ticket_id, staff=False)
    payload = conversation.model_dump_json()
    assert RESOLUTION in payload and "Public reply" in payload
    assert "ESCALATION-REASON" not in payload and "RETURN-REASON" not in payload
    assert "internal_reason" not in payload and "history" not in payload
    listed = workflow.list_own(customer)
    assert "ESCALATION-REASON" not in str(listed) and "RETURN-REASON" not in str(listed)
    staff_view = workflow.read(agent, ticket_id, staff=True)
    assert "internal_reason" in staff_view.model_dump_json()  # staff projection keeps them


def test_adm05_a_suspended_customer_cannot_read_the_resolution_until_reactivated() -> None:
    from finance_ai.core.errors import StaffAccessDeniedError

    admin, agent, customer, _other = support_world()
    tickets, workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")
    tickets.resolve(admin_id=admin, ticket_id=ticket_id, summary=RESOLUTION, correlation_id="c")
    assert call_suspension(admin, customer, True, "chargeback review") == "ok"

    with pytest.raises(StaffAccessDeniedError):
        workflow.read(customer, ticket_id, staff=False)
    with pytest.raises(StaffAccessDeniedError):
        workflow.list_own(customer)

    assert call_suspension(admin, customer, False, None) == "ok"
    assert RESOLUTION in workflow.read(customer, ticket_id, staff=False).model_dump_json()


def test_adm05_originating_agent_keeps_read_only_access_while_escalated() -> None:
    from finance_ai.core.errors import SupportTicketStateConflictError

    admin, agent, customer, _other = support_world()
    tickets, workflow, support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")

    detail = support.get_assigned(staff_user(agent, "support_agent"), ticket_id)
    conversation = workflow.read(agent, ticket_id, staff=True)

    assert detail.id == ticket_id and conversation.ticket.status == "escalated"
    with pytest.raises(SupportTicketStateConflictError):
        workflow.message(agent, ticket_id, "I will keep working", "c", staff=True)
    with pytest.raises(SupportTicketStateConflictError):
        workflow.transition(agent, ticket_id, "resolve it", "c", escalate=False)


def test_adm05_return_is_atomic_and_rejects_blank_reasons_without_any_change() -> None:
    from finance_ai.core.errors import AdminRequestInvalidError, SupportTicketStateConflictError

    admin, agent, customer, _other = support_world()
    tickets, _workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")
    before_activity = activity_types(ticket_id)

    with pytest.raises(AdminRequestInvalidError):
        tickets.return_to_support(
            admin_id=admin, ticket_id=ticket_id, reason="   ", correlation_id="c"
        )

    assert ticket_row(ticket_id)["status"] == "escalated"
    assert ticket_row(ticket_id)["admin_owner_user_id"] == admin
    assert activity_types(ticket_id) == before_activity
    tickets.return_to_support(admin_id=admin, ticket_id=ticket_id, reason="ok", correlation_id="c")
    with pytest.raises(SupportTicketStateConflictError):
        tickets.return_to_support(
            admin_id=admin, ticket_id=ticket_id, reason="again", correlation_id="c"
        )


def test_adm05_database_refuses_an_owner_on_a_ticket_that_is_not_escalated() -> None:
    from finance_ai.support.schemas import AgentTicketCreate

    admin, agent, customer, _other = support_world()
    _tickets, workflow, _support = ticket_services()
    ticket = workflow.create(
        agent,
        AgentTicketCreate(
            subject="Plain", category="general", description="d", customer_identifier=str(customer)
        ),
        "c",
        staff=True,
    )
    with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text("UPDATE support_ticket SET admin_owner_user_id = :a WHERE id = :id"),
                {"a": admin, "id": ticket.id},
            )
        session.rollback()


def test_adm05_audit_records_the_transitions_without_conversation_text() -> None:
    admin, agent, customer, _other = support_world()
    tickets, _workflow, _support = ticket_services()
    ticket_id = make_escalated_ticket(agent, customer)
    tickets.claim(admin_id=admin, ticket_id=ticket_id, correlation_id="c")
    tickets.reply(admin_id=admin, ticket_id=ticket_id, body="SECRET-BODY text", correlation_id="c")
    tickets.return_to_support(
        admin_id=admin, ticket_id=ticket_id, reason=RETURN_REASON, correlation_id="c"
    )

    rendered = str(audit_events())
    assert "SECRET-BODY" not in rendered and "RETURN-REASON" not in rendered
    assert "ESCALATION-REASON" not in rendered
    admin_events = [event for event in audit_events() if event[2] == str(admin)]
    assert [event[0] for event in admin_events] == [
        "support.ticket_claimed",
        "support.message_sent",
        "support.ticket_returned",
    ]
    assert all(event[3] == str(ticket_id) for event in admin_events)


# --- SEC-01 recovery on PostgreSQL: row locks, single use, session revocation ------------------

RECOVERY_PASSWORD = "Recovery-passphrase-2026"


def recovery_world():  # type: ignore[no-untyped-def]
    from finance_ai.audit.service import SecurityAuditWriter
    from finance_ai.auth.credentials import CredentialCodec
    from finance_ai.auth.dependencies import get_password_hasher
    from finance_ai.auth.session_policy import SystemClock
    from finance_ai.db.models.admin import AdminTotpFactor
    from finance_ai.staff_auth.recovery import StaffRecoveryService
    from tests.unit.test_staff_auditor_auth_service import build_policy

    hasher = get_password_hasher()
    policy = build_policy()
    admin = make_user("admin", "admin@example.test")
    with get_session_factory()() as session:
        session.execute(
            text("UPDATE app_user SET password_hash = :hash WHERE id = :id"),
            {"hash": hasher.hash(RECOVERY_PASSWORD), "id": admin},
        )
        secret = policy.verifier.new_secret()
        session.add(
            AdminTotpFactor(
                admin_user_id=admin,
                secret_ciphertext=policy.cipher.encrypt(secret, user_id=admin),
                state="active",
                activated_at=datetime.now(UTC),
            )
        )
        session.commit()
    service = StaffRecoveryService(
        session_factory=get_session_factory(),
        password_hasher=hasher,
        credential_codec=CredentialCodec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
        mfa=policy,
    )
    return admin, secret, service


def test_recovery_codes_are_issued_on_postgres_with_hashes_only() -> None:
    from finance_ai.core.errors import PasswordConfirmationInvalidError

    admin, _secret, service = recovery_world()

    with pytest.raises(PasswordConfirmationInvalidError):
        service.issue_recovery_codes(admin, "wrong-password", correlation_id="c0")
    assert scalar("SELECT count(*) FROM staff_mfa_recovery_code") == 0

    codes = service.issue_recovery_codes(admin, RECOVERY_PASSWORD, correlation_id="c1")
    replaced = service.issue_recovery_codes(admin, RECOVERY_PASSWORD, correlation_id="c2")

    assert len(codes) == len(replaced) == 10 and set(codes).isdisjoint(replaced)
    assert scalar("SELECT count(*) FROM staff_mfa_recovery_code WHERE consumed_at IS NULL") == 10
    assert scalar("SELECT count(*) FROM staff_mfa_recovery_code") == 20
    stored = str(audit_events())
    assert replaced[0] not in stored and "recovery_codes_issued" in stored


def test_recovery_code_resets_the_authenticator_and_revokes_sessions_on_postgres() -> None:
    from finance_ai.core.errors import MfaRecoveryInvalidError

    admin, _secret, service = recovery_world()
    session_id = make_session(admin, "r")
    codes = service.issue_recovery_codes(admin, RECOVERY_PASSWORD, correlation_id="c1")

    challenge = service.reset_authenticator(
        email="admin@example.test",
        password=RECOVERY_PASSWORD,
        recovery_code=codes[0],
        totp_code=None,
        correlation_id="c2",
    )

    assert challenge.enrollment_required is True
    assert (
        scalar("SELECT state FROM admin_totp_factor WHERE admin_user_id = :id", id=admin)
        == "disabled"
    )
    assert (
        scalar("SELECT revocation_reason FROM user_session WHERE id = :id", id=session_id)
        == "mfa_reset"
    )
    assert scalar("SELECT count(*) FROM staff_mfa_recovery_code WHERE consumed_at IS NULL") == 0
    assert scalar("SELECT count(*) FROM staff_mfa_challenge WHERE consumed_at IS NULL") == 1
    assert ("auth.mfa_recovered", "success") in [event[:2] for event in audit_events()]
    with pytest.raises(MfaRecoveryInvalidError):
        service.reset_authenticator(
            email="admin@example.test",
            password=RECOVERY_PASSWORD,
            recovery_code=codes[0],
            totp_code=None,
            correlation_id="c3",
        )


def test_authenticator_code_replaces_the_authenticator_on_postgres() -> None:
    import pyotp

    admin, secret, service = recovery_world()

    challenge = service.reset_authenticator(
        email="admin@example.test",
        password=RECOVERY_PASSWORD,
        recovery_code=None,
        totp_code=pyotp.TOTP(secret).now(),
        correlation_id="c1",
    )

    assert challenge.enrollment_required is True
    assert (
        scalar("SELECT state FROM admin_totp_factor WHERE admin_user_id = :id", id=admin)
        == "disabled"
    )
    assert ("auth.mfa_replaced", "success") in [event[:2] for event in audit_events()]


def test_a_recovery_code_resets_exactly_once_under_concurrency() -> None:
    from finance_ai.core.errors import MfaRecoveryInvalidError

    admin, _secret, service = recovery_world()
    codes = service.issue_recovery_codes(admin, RECOVERY_PASSWORD, correlation_id="c1")
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            service.reset_authenticator(
                email="admin@example.test",
                password=RECOVERY_PASSWORD,
                recovery_code=codes[1],
                totp_code=None,
                correlation_id=f"c-{index}",
            )
            result = "reset"
        except MfaRecoveryInvalidError:
            result = "rejected"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["rejected", "rejected", "rejected", "reset"]
    assert (
        scalar("SELECT count(*) FROM security_audit_event WHERE event_type = 'auth.mfa_recovered'")
        == 1
    )


def test_downgrade_refuses_to_drop_suspension_history() -> None:
    admin = make_user("admin", "admin@example.test")
    customer = make_user("user", "customer@example.test")
    assert call_suspension(admin, customer, True, "evidence that must survive") == "ok"
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    with pytest.raises(RuntimeError, match="suspension history"):
        alembic_command.downgrade(config, "0019_staff_mfa_challenge")

    # The failed downgrade rolled back: schema, routines and the evidence are intact.
    assert scalar("SELECT count(*) FROM account_suspension") == 1
    assert (
        scalar("SELECT count(*) FROM pg_proc WHERE proname = 'sp_admin_set_account_suspension'")
        == 1
    )
