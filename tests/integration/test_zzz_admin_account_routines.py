"""PostgreSQL proof of the account routines: triggers T1/T2 and procedure P1 (ADM-02/ADM-04)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
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


@pytest.fixture(autouse=True)
def clean_rows(migrated_database: None) -> Iterator[None]:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE TABLE security_audit_event, account_suspension, app_user "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
    yield


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


def test_p1_concurrent_mutual_suspension_leaves_exactly_one_active_administrator() -> None:
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
    assert results["two"] in {"last_admin", "actor_not_authorized"}
    assert (
        scalar(
            "SELECT count(*) FROM app_user WHERE is_active AND role_id = "
            "(SELECT id FROM role WHERE name = 'admin')"
        )
        == 1
    )
    assert scalar("SELECT count(*) FROM account_suspension") == 1


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
