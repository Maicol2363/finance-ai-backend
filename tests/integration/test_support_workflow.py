from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.dependencies import get_password_hasher
from finance_ai.core.errors import (
    StaffAccessDeniedError,
    SupportTicketNotFoundError,
    SupportTicketStateConflictError,
)
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.support import SupportTicket, SupportTicketActivity
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.main import create_app
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.schemas import AgentTicketCreate, TicketCreate
from finance_ai.support.service import SupportTicketService
from finance_ai.support.workflow import TicketWorkflow

"""PostgreSQL claim atomicity checks for SUP-01."""


pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def migrated_test_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    from finance_ai.core.config import get_settings

    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_support_audit_events(migrated_test_database: None):
    yield
    with get_session_factory()() as session:
        # Migration fixtures downgrade this isolated database between modules.
        session.execute(text("TRUNCATE TABLE security_audit_event"))
        session.commit()


@pytest.fixture
def people():
    with get_session_factory()() as session:
        roles = {r.name: r.id for r in session.scalars(select(Role))}
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        ids = []
        password_hash = get_password_hasher().hash("Fictional-Test-Only-728!")
        for role in ("user", "user", "support_agent", "support_agent", "admin"):
            user = AppUser(
                email=f"pb012-{uuid4()}@example.test",
                password_hash=password_hash,
                role_id=roles[role],
                preferred_currency_id=currency,
            )
            session.add(user)
            session.flush()
            ids.append((user.id, user.email))
        now = datetime.now(UTC)
        for agent in ids[2:4]:
            session.add(
                StaffInvitation(
                    email=agent[1],
                    role_id=roles["support_agent"],
                    token_hash=uuid4().hex + uuid4().hex,
                    created_by_admin_user_id=ids[4][0],
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    consumed_at=now,
                    activated_user_id=agent[0],
                )
            )
        session.commit()
    return ids


def make_ticket(people):
    customer, _, agent, _, _ = people
    workflow = TicketWorkflow(get_session_factory())
    ticket = workflow.create(
        customer[0],
        TicketCreate(subject="Ayuda", category="general", description="Descripción de prueba"),
        "create-test",
        staff=False,
    )
    service = SupportTicketService(get_session_factory(), SecurityAuditWriter())
    service.claim(
        StaffSessionUser(id=agent[0], email=agent[1], role="support_agent"), ticket.id, "claim-test"
    )
    return workflow, ticket.id


def test_customer_agent_exchange_and_customer_visible_resolution(people):
    customer, other_customer, agent, other_agent, admin = people
    workflow, ticket_id = make_ticket(people)
    workflow.message(
        customer[0], ticket_id, "<script>texto literal</script>", "message-1", staff=False
    )
    workflow.message(agent[0], ticket_id, "Respuesta del agente", "message-2", staff=True)
    view = workflow.read(agent[0], ticket_id, staff=True)
    assert view.customer.model_dump() == {
        "id": customer[0],
        "email": customer[1],
        "is_active": True,
    }
    assert [m.author_user_id for m in view.messages] == [customer[0], agent[0]]
    assert [m.created_at for m in view.messages] == sorted(m.created_at for m in view.messages)
    assert [h.event_type for h in view.history] == [
        "ticket_created",
        "ticket_claimed",
        "message_sent",
        "message_sent",
    ]
    for actor, staff in ((other_customer[0], False), (other_agent[0], True)):
        with pytest.raises(SupportTicketNotFoundError):
            workflow.read(actor, ticket_id, staff=staff)
        with pytest.raises(SupportTicketNotFoundError):
            workflow.message(actor, ticket_id, "denied", "denied", staff=staff)
    with pytest.raises(StaffAccessDeniedError):
        workflow.read(admin[0], ticket_id, staff=True)
    workflow.transition(agent[0], ticket_id, "Solución comunicada", "resolve-1", escalate=False)
    customer_view = workflow.read(customer[0], ticket_id, staff=False).model_dump(mode="json")
    assert customer_view["ticket"]["resolution_summary"] == "Solución comunicada"
    assert customer_view["ticket"]["status"] == "resolved"
    assert set(customer_view) == {"ticket", "messages"}
    for actor, staff in ((customer[0], False), (agent[0], True)):
        with pytest.raises(SupportTicketStateConflictError):
            workflow.message(actor, ticket_id, "late", "late", staff=staff)
    with pytest.raises(SupportTicketStateConflictError):
        workflow.transition(agent[0], ticket_id, "again", "again", escalate=False)
    with get_session_factory()() as session:
        events = session.scalars(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.target_resource_id == str(ticket_id)
            )
        ).all()
        assert len(events) == 5
        assert all(e.context == {} for e in events)
        assert "Respuesta" not in str([e.context for e in events])


def test_escalation_read_only_internal_reason_and_origin_preserved(people):
    customer, _, agent, other_agent, _ = people
    workflow, ticket_id = make_ticket(people)
    with pytest.raises(SupportTicketNotFoundError):
        workflow.transition(other_agent[0], ticket_id, "denied", "denied", escalate=True)
    workflow.transition(agent[0], ticket_id, "INTERNAL-REASON-SECRET", "escalate", escalate=True)
    assert workflow.read(agent[0], ticket_id, staff=True).ticket.status == "escalated"
    assert (
        "INTERNAL-REASON-SECRET"
        not in workflow.read(customer[0], ticket_id, staff=False).model_dump_json()
    )
    for actor, staff in ((agent[0], True), (customer[0], False)):
        with pytest.raises(SupportTicketStateConflictError):
            workflow.message(actor, ticket_id, "denied", "denied", staff=staff)
    with pytest.raises(SupportTicketStateConflictError):
        workflow.transition(agent[0], ticket_id, "denied", "denied", escalate=False)
    with get_session_factory()() as session:
        ticket = session.get(SupportTicket, ticket_id)
        assert ticket.originating_support_agent_id == agent[0]
        activity = session.scalar(
            select(SupportTicketActivity).where(
                SupportTicketActivity.ticket_id == ticket_id,
                SupportTicketActivity.event_type == "ticket_escalated",
            )
        )
        assert activity.internal_reason == "INTERNAL-REASON-SECRET"
        audit = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.target_resource_id == str(ticket_id),
                SecurityAuditEvent.event_type == "ticket.escalated",
            )
        )
        assert audit is not None and audit.context == {}


def test_exact_lookup_and_agent_creation(people):
    customer, _, agent, _, _ = people
    workflow = TicketWorkflow(get_session_factory())
    assert workflow.lookup(agent[0], customer[1].upper(), "lookup").id == customer[0]
    assert workflow.lookup(agent[0], str(customer[0]), "lookup").id == customer[0]
    for identifier in ("pb012", "%", agent[1], "99999999999999999999999999999999999"):
        with pytest.raises(SupportTicketNotFoundError):
            workflow.lookup(agent[0], identifier, "lookup")
    with pytest.raises(StaffAccessDeniedError):
        workflow.lookup(customer[0], customer[1], "lookup")
    ticket = workflow.create(
        agent[0],
        AgentTicketCreate(
            subject="Creado por agente",
            category="technical",
            description="Descripción",
            customer_identifier=customer[1],
        ),
        "create",
        staff=True,
    )
    assert ticket.status == "open" and ticket.assigned_staff_user_id is None
    assert workflow.list_own(customer[0])[0].id == ticket.id
    with pytest.raises(SupportTicketNotFoundError):
        workflow.read(agent[0], ticket.id, staff=True)
    with get_session_factory()() as session:
        saved = session.get(SupportTicket, ticket.id)
        assert saved.created_by_user_id == agent[0] and saved.customer_user_id == customer[0]


def test_concurrent_terminal_transitions_have_single_winner(people):
    _, _, agent, _, _ = people
    workflow, ticket_id = make_ticket(people)
    barrier = Barrier(2)

    def attempt(escalate):
        barrier.wait(timeout=10)
        try:
            workflow.transition(agent[0], ticket_id, "Resultado", "race", escalate=escalate)
            return "success"
        except SupportTicketStateConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [True, False])) == ["conflict", "success"]
    with get_session_factory()() as session:
        assert (
            len(
                session.scalars(
                    select(SupportTicketActivity).where(
                        SupportTicketActivity.ticket_id == ticket_id
                    )
                ).all()
            )
            == 3
        )


def test_audit_failure_rolls_back_message_and_transition(people, monkeypatch):
    _, _, agent, _, _ = people
    workflow, ticket_id = make_ticket(people)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(workflow.audit, "append", fail)
    with pytest.raises(RuntimeError):
        workflow.message(agent[0], ticket_id, "not committed", "rollback", staff=True)
    with pytest.raises(RuntimeError):
        workflow.transition(agent[0], ticket_id, "not committed", "rollback", escalate=False)
    view = workflow.read(agent[0], ticket_id, staff=True)
    assert view.messages == [] and len(view.history) == 2 and view.ticket.status == "in_progress"


def test_real_http_sessions_resource_authorization_and_validation(people, monkeypatch):
    from finance_ai.core.config import get_settings

    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", '["https://staff.example.test"]')
    get_settings.cache_clear()
    customer, other, agent, _other_agent, _admin = people
    app = create_app()
    headers = {
        "X-Staff-Request": "1",
        "Sec-Fetch-Site": "same-origin",
        "Origin": "https://staff.example.test",
    }
    staff = TestClient(app, headers=headers, base_url="https://staff.example.test")
    mobile = TestClient(app)

    def login(person):
        response = mobile.post(
            "/api/v1/auth/login",
            json={
                "email": person[1],
                "password": "Fictional-Test-Only-728!",
                "platform": "android",
            },
        )
        assert response.status_code == 200, response.text
        return {"Authorization": "Bearer " + response.json()["access_credential"]}

    customer_headers = login(customer)
    other_headers = login(other)
    signed = staff.post(
        "/api/v1/staff/auth/login", json={"email": agent[1], "password": "Fictional-Test-Only-728!"}
    )
    assert signed.status_code == 200, signed.text
    payload = {"subject": "API", "category": "general", "description": "Texto"}
    created = mobile.post("/api/v1/support/tickets", json=payload, headers=customer_headers)
    assert created.status_code == 201, created.text
    ticket_id = created.json()["id"]
    path = f"/api/v1/support/tickets/{ticket_id}"
    staff_path = f"/api/v1/staff/support/tickets/{ticket_id}"
    assert mobile.get(path).status_code == 401
    assert mobile.get(path, headers=other_headers).status_code == 404
    assert (
        mobile.post(path + "/messages", headers=other_headers, json={"body": "bad"}).status_code
        == 404
    )
    assert mobile.get(staff_path, headers=customer_headers).status_code == 403
    assert staff.get(staff_path + "/conversation").status_code == 404
    assert staff.post(staff_path + "/claim", json={}).status_code == 200
    assert staff.get(staff_path + "/conversation").status_code == 200
    for body in ("", "   ", "x" * 4001):
        response = mobile.post(path + "/messages", headers=customer_headers, json={"body": body})
        assert response.status_code == 422
        assert body not in response.text if len(body) > 5 else True
    assert (
        mobile.post(
            path + "/messages",
            headers=customer_headers,
            json={"body": "hola", "author_user_id": other[0]},
        ).status_code
        == 422
    )
    assert (
        mobile.post(
            "/api/v1/support/tickets",
            headers=customer_headers,
            json={**payload, "customer_user_id": other[0]},
        ).status_code
        == 422
    )
    assert (
        mobile.post(
            "/api/v1/support/tickets",
            headers=customer_headers,
            json={**payload, "category": "invalid"},
        ).status_code
        == 422
    )
    assert (
        mobile.post(
            path + "/messages", headers=customer_headers, json={"body": "Cliente real"}
        ).status_code
        == 204
    )
    assert staff.post(staff_path + "/messages", json={"body": "Agente real"}).status_code == 204
    assert staff.post(staff_path + "/resolve", json={"summary": "  "}).status_code == 422
    assert staff.post(staff_path + "/resolve", json={"summary": "Solucionado"}).status_code == 204
    response = mobile.get(path, headers=customer_headers)
    assert response.json()["ticket"]["resolution_summary"] == "Solucionado"
    assert len(response.json()["messages"]) == 2
    assert (
        mobile.post(
            path + "/messages", headers=customer_headers, json={"body": "tarde"}
        ).status_code
        == 409
    )
    assert staff.post(staff_path + "/escalate", json={"summary": "tarde"}).status_code == 409
    assert (
        staff.post(
            "/api/v1/staff/support/tickets/customer-lookup", json={"identifier": customer[1]}
        ).status_code
        == 200
    )
    with get_session_factory()() as session:
        session.execute(update(AppUser).where(AppUser.id == customer[0]).values(is_active=False))
        session.commit()
    assert mobile.get(path, headers=customer_headers).status_code == 401
    assert staff.post("/api/v1/staff/auth/logout", json={}).status_code == 204
    assert staff.get(staff_path + "/conversation").status_code == 401


def test_originating_agent_retains_read_after_administrative_assignment_changes(people):
    _, _, agent, _, admin = people
    workflow, ticket_id = make_ticket(people)
    workflow.transition(agent[0], ticket_id, "Internal reason", "escalate", escalate=True)
    with get_session_factory()() as session:
        session.execute(
            update(SupportTicket)
            .where(SupportTicket.id == ticket_id)
            .values(assigned_staff_user_id=admin[0])
        )
        session.commit()
    assert workflow.read(agent[0], ticket_id, staff=True).ticket.status == "escalated"
    assert (
        SupportTicketService(get_session_factory(), SecurityAuditWriter())
        .get_assigned(
            StaffSessionUser(id=agent[0], email=agent[1], role="support_agent"), ticket_id
        )
        .status
        == "escalated"
    )
    with pytest.raises(SupportTicketStateConflictError):
        workflow.message(agent[0], ticket_id, "denied", "denied", staff=True)


def test_concurrent_message_and_resolution_preserve_terminal_boundary(people):
    customer, _, agent, _, _ = people
    workflow, ticket_id = make_ticket(people)
    barrier = Barrier(2)

    def send():
        barrier.wait(timeout=10)
        try:
            workflow.message(
                customer[0], ticket_id, "Concurrent message", "message-race", staff=False
            )
            return True
        except SupportTicketStateConflictError:
            return False

    def resolve():
        barrier.wait(timeout=10)
        workflow.transition(
            agent[0], ticket_id, "Concurrent resolution", "resolve-race", escalate=False
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(send)
        resolved = pool.submit(resolve)
        was_sent = sent.result(timeout=15)
        resolved.result(timeout=15)
    view = workflow.read(agent[0], ticket_id, staff=True)
    assert view.ticket.status == "resolved"
    assert len(view.messages) == int(was_sent)
    assert view.history[-1].event_type == "ticket_resolved"
    with pytest.raises(SupportTicketStateConflictError):
        workflow.message(customer[0], ticket_id, "After resolution", "late", staff=False)
