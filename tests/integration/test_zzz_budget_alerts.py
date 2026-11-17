"""PostgreSQL proof of the budget alerts (EML-03) through the real expense and budget services:
what is queued, what is not, and that simultaneous expenses never miss or repeat a crossing."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from finance_ai.budgets.dependencies import get_budget_service
from finance_ai.core.config import Settings, get_settings
from finance_ai.db.expense_uow import SQLAlchemyExpenseUnitOfWork
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.expenses.schemas import CreateExpenseCommand, ExpenseUpdateCommand
from finance_ai.expenses.service import ExpenseService
from finance_ai.notifications import budget_alerts
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.messages import EmailMessage, SendResult

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
LIMIT = Decimal("600000.00")


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
                "TRUNCATE TABLE notification_outbox, notification_preference, "
                "email_verification, budget, expense, receipt, financial_transaction, "
                "account, category, app_user RESTART IDENTITY CASCADE"
            )
        )
        session.commit()


@pytest.fixture(autouse=True)
def clean_rows(migrated_database: None) -> Iterator[None]:
    reset_rows()
    yield
    reset_rows()


def sql(statement: str, **params: object) -> Any:
    with get_session_factory()() as session:
        result = session.execute(text(statement), params)
        value = result.all() if result.returns_rows else None
        session.commit()
        return value


class Person:
    def __init__(
        self,
        *,
        verified: bool = True,
        enabled: bool = True,
        alerts: bool = True,
        preference_row: bool = True,
        limit: str = "600000.00",
    ) -> None:
        self.user_id = int(
            sql(
                "INSERT INTO app_user (email, password_hash, role_id, preferred_currency_id, "
                "email_verified_at) VALUES ('ana@example.com', 'hash', "
                "(SELECT id FROM role WHERE name = 'user'), "
                "(SELECT id FROM currency WHERE code = 'COP'), "
                "CASE WHEN :v THEN now() END) RETURNING id",
                v=verified,
            )[0][0]
        )
        if preference_row:
            sql(
                "INSERT INTO notification_preference (user_id, email_enabled, budget_alerts) "
                "VALUES (:u, :e, :a)",
                u=self.user_id,
                e=enabled,
                a=alerts,
            )
        self.account_id = int(
            sql(
                "INSERT INTO account (user_id, name, type, currency_id, opening_balance, "
                "is_active) VALUES (:u, 'Efectivo', 'cash', "
                "(SELECT id FROM currency WHERE code = 'COP'), 0, true) RETURNING id",
                u=self.user_id,
            )[0][0]
        )
        self.budget_id = int(
            sql(
                "INSERT INTO budget (user_id, currency_id, name, amount, schedule_type, "
                "frequency_unit, frequency_interval, starts_on, is_active) VALUES (:u, "
                "(SELECT id FROM currency WHERE code = 'COP'), 'Mercado', :a, 'recurring', "
                "'month', 1, date_trunc('month', now() AT TIME ZONE 'UTC')::date, true) "
                "RETURNING id",
                u=self.user_id,
                a=limit,
            )[0][0]
        )
        self.service = ExpenseService(
            uow_factory=lambda: SQLAlchemyExpenseUnitOfWork(get_session_factory())
        )

    def spend(self, amount: str, merchant: str = "Tienda") -> int:
        return self.service.create(
            owner_id=self.user_id,
            command=CreateExpenseCommand(
                account_id=self.account_id,
                amount=Decimal(amount),
                merchant=merchant,
                occurred_at=datetime.now(UTC),
            ),
        ).id

    def alerts(self) -> list[tuple[int, str, str | None]]:
        rows = sql(
            "SELECT (payload->>'threshold')::int, status, last_error_code "
            "FROM notification_outbox WHERE kind = 'budget_alert' ORDER BY id"
        )
        return [(row[0], row[1], row[2]) for row in rows]


# --- what is queued ------------------------------------------------------------------------


def test_nothing_is_queued_below_eighty_percent() -> None:
    person = Person()

    person.spend("479999.00")

    assert person.alerts() == []


def test_crossing_eighty_percent_queues_one_alert_with_the_screens_numbers() -> None:
    person = Person()

    person.spend("300000.00")
    person.spend("200000.00")

    [(threshold, status, _)] = person.alerts()
    assert (threshold, status) == (80, "pending")
    payload = sql("SELECT payload FROM notification_outbox")[0][0]
    on_screen = get_budget_service().get(owner_id=person.user_id, budget_id=person.budget_id)
    assert Decimal(payload["spent"]) == on_screen.spent_amount == Decimal("500000.00")
    assert Decimal(payload["limit"]) == LIMIT and payload["currency_code"] == "COP"
    assert "ana@example.com" not in str(payload)


def test_more_spending_under_the_next_threshold_does_not_repeat_the_alert() -> None:
    person = Person()
    person.spend("500000.00")

    person.spend("50000.00")
    person.spend("10000.00")

    assert person.alerts() == [(80, "pending", None)]


def test_reaching_the_limit_withdraws_a_waiting_eighty_percent_email() -> None:
    person = Person()
    person.spend("500000.00")

    person.spend("100000.00")

    assert person.alerts() == [(80, "skipped", "superseded"), (100, "pending", None)]


def test_one_expense_that_jumps_both_thresholds_sends_only_the_higher() -> None:
    person = Person()

    person.spend("900000.00")

    assert sorted(person.alerts()) == [(80, "skipped", "superseded"), (100, "pending", None)]


def test_raising_an_expense_with_an_edit_can_cross_a_threshold() -> None:
    person = Person()
    expense_id = person.spend("100000.00")
    assert person.alerts() == []

    person.service.update(
        owner_id=person.user_id,
        expense_id=expense_id,
        command=ExpenseUpdateCommand(fields=frozenset({"amount"}), amount=Decimal("520000.00")),
    )

    assert person.alerts() == [(80, "pending", None)]


def test_changing_the_limit_re_arms_the_alert() -> None:
    person = Person()
    person.spend("500000.00")
    assert person.alerts() == [(80, "pending", None)]

    sql("UPDATE budget SET amount = 550000 WHERE id = :b", b=person.budget_id)
    person.spend("1000.00")  # 501 000 of 550 000 is 91 %: a different limit, a different key

    assert person.alerts() == [(80, "pending", None), (80, "pending", None)]


# --- who is not emailed --------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {"enabled": False},
        {"alerts": False},
        {"verified": False},
        {"preference_row": False},
    ],
)
def test_nothing_is_queued_for_someone_who_has_not_opted_in(options: dict[str, bool]) -> None:
    person = Person(**options)

    person.spend("900000.00")

    assert person.alerts() == []


def test_an_inactive_budget_is_not_warned_about() -> None:
    person = Person()
    sql("UPDATE budget SET is_active = false WHERE id = :b", b=person.budget_id)

    person.spend("900000.00")

    assert person.alerts() == []


# --- failure isolation ---------------------------------------------------------------------


def test_a_failure_while_deciding_never_stops_the_expense_from_being_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = Person()

    def explode(self: object, user_id: int) -> int:
        raise RuntimeError("alert rules broke")

    monkeypatch.setattr(budget_alerts.BudgetAlertEvaluator, "evaluate", explode)

    person.spend("900000.00")

    assert sql("SELECT count(*) FROM expense")[0][0] == 1
    assert person.alerts() == []


def test_a_failure_inside_the_evaluator_is_rolled_back_with_its_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = Person()
    original = budget_alerts.enqueue

    def enqueue_then_fail(*args: Any, **kwargs: Any) -> bool:
        original(*args, **kwargs)
        raise RuntimeError("after writing the row")

    monkeypatch.setattr(budget_alerts, "enqueue", enqueue_then_fail)

    person.spend("900000.00")

    assert sql("SELECT count(*) FROM expense")[0][0] == 1
    assert sql("SELECT count(*) FROM notification_outbox")[0][0] == 0  # half a decision is undone


# --- concurrency ---------------------------------------------------------------------------


def race(count: int, action: Callable[[int], Any]) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def run(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            results[index] = action(index)
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    return results


def test_simultaneous_expenses_that_together_cross_a_threshold_do_not_miss_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = Person()
    # Force the worst timing: every request has saved its expense but none has committed when
    # the decisions are made. Without serialising the decisions each would see only its own
    # 11 % of the limit and the crossing would be missed.
    all_inserted = threading.Barrier(8)
    original = budget_alerts.BudgetAlertEvaluator.evaluate

    def evaluate_together(self: budget_alerts.BudgetAlertEvaluator, user_id: int) -> int:
        all_inserted.wait(timeout=20)
        return original(self, user_id)

    monkeypatch.setattr(budget_alerts.BudgetAlertEvaluator, "evaluate", evaluate_together)

    # Distinct merchants: simultaneous first use of one new merchant name is a separate, existing
    # race in the store table. Eight times 70 000 reach 93 % of the limit.
    results = race(8, lambda i: person.spend("70000.00", f"Tienda {i}"))

    assert not [r for r in results if isinstance(r, Exception)], results
    assert sql("SELECT sum(amount) FROM expense")[0][0] == Decimal("560000.00")
    assert person.alerts() == [(80, "pending", None)]


def test_simultaneous_expenses_that_all_cross_the_limit_queue_it_exactly_once() -> None:
    person = Person()

    results = race(
        6, lambda i: person.spend("200000.00", f"Tienda {i}")
    )  # 1 200 000: twice the limit

    assert not [r for r in results if isinstance(r, Exception)], results
    alerts = person.alerts()
    assert alerts.count((100, "pending", None)) == 1
    assert [a for a in alerts if a[0] == 100 and a[1] != "skipped"] == [(100, "pending", None)]
    assert all(status in {"pending", "skipped"} for _, status, _ in alerts)


# --- end to end ----------------------------------------------------------------------------


def test_the_queued_alert_is_delivered_as_an_email_with_the_right_numbers() -> None:
    person = Person()
    person.spend("510000.00")
    sent: list[EmailMessage] = []

    class Recorder:
        def send(self, message: EmailMessage) -> SendResult:
            sent.append(message)
            return SendResult(provider_message_id="id-1@finance.example.com")

    dispatcher = NotificationDispatcher(
        session_factory=get_session_factory(),
        sender=Recorder(),
        settings=Settings(_env_file=None, email_from_address="avisos@finance.example.com"),
        clock=lambda: datetime.now(UTC),
    )

    summary = dispatcher.dispatch_once()

    assert summary.sent == 1
    [message] = sent
    assert message.to_address == "ana@example.com"
    assert "Mercado" in message.subject and "85" in message.subject
    assert "510.000" in message.text_body and "600.000" in message.text_body
