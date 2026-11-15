"""Fictional Administrator fixture; never run against an ordinary or production database.

Creates two administrators (MFA not yet enrolled), two support agents, two customers and a set
of tickets (open, escalated, resolved) so the Administrator workspace can be reviewed and tested.
Safety rules, like the Support demo: environment must be local/test and the database must be
named ``admin_demo_test``. Credentials go to a 0600 file, never to the console.

    FINANCE_AI_DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5432/admin_demo_test \
        uv run alembic upgrade head
    FINANCE_AI_DATABASE_URL=... uv run python scripts/seed_admin_demo.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex, token_urlsafe

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from finance_ai.admin.provisioning import AdminProvisioner
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.dependencies import get_password_hasher, get_unsafe_password_provider
from finance_ai.core.config import get_settings
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.support import SupportTicket
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.schemas import TicketCreate
from finance_ai.support.service import SupportTicketService
from finance_ai.support.workflow import TicketWorkflow

DEMO_DATABASE = "admin_demo_test"
CREDENTIALS = Path(os.environ.get("ADMIN_DEMO_CREDENTIALS", "demo-data/admin-credentials.txt"))
ADMINS = ("admin1@example.com", "admin2@example.com")
AGENTS = ("agente1@example.com", "agente2@example.com")
CUSTOMERS = ("cliente1@example.com", "cliente2@example.com")


def seed() -> None:
    settings = get_settings()
    if (
        settings.environment not in {"local", "test"}
        or make_url(settings.database_url).database != DEMO_DATABASE
    ):
        raise SystemExit(
            f"The Administrator demo requires local/test and the isolated {DEMO_DATABASE} database"
        )
    factory = get_session_factory()
    with factory() as session:
        seed_shared_data(session)
        if session.scalar(select(func.count()).select_from(AppUser)):
            raise SystemExit("Demo data already exists; reset only the isolated demo database")

    password = token_urlsafe(24)
    hasher = get_password_hasher()
    provisioner = AdminProvisioner(
        session_factory=factory,
        password_hasher=hasher,
        unsafe_passwords=get_unsafe_password_provider(),
        audit_writer=SecurityAuditWriter(),
    )
    admin_ids = [
        provisioner.create_admin(email=email, password=password).user_id for email in ADMINS
    ]

    with factory.begin() as session:
        roles = dict(session.execute(select(Role.name, Role.id)).all())
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency is not None
        password_hash = hasher.hash(password)
        now = datetime.now(UTC)
        people: dict[str, AppUser] = {}
        for email, role in [(e, "support_agent") for e in AGENTS] + [
            (e, "user") for e in CUSTOMERS
        ]:
            user = AppUser(
                email=email,
                password_hash=password_hash,
                role_id=roles[role],
                preferred_currency_id=currency,
                is_active=True,
            )
            session.add(user)
            session.flush()
            people[email] = user
        for email in AGENTS:
            agent = people[email]
            session.add(
                StaffInvitation(
                    email=email,
                    role_id=agent.role_id,
                    token_hash=token_hex(32),
                    created_by_admin_user_id=admin_ids[0],
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    consumed_at=now,
                    activated_user_id=agent.id,
                )
            )
        agent_ids = [people[email].id for email in AGENTS]
        customer_ids = [people[email].id for email in CUSTOMERS]

    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.touch(mode=0o600, exist_ok=True)
    CREDENTIALS.chmod(0o600)
    CREDENTIALS.write_text(
        "DATOS FICTICIOS LOCALES - NO COPIAR A PR, CAPTURAS NI LOGS\n"
        f"Administradores: {', '.join(ADMINS)}\n"
        f"Agentes: {', '.join(AGENTS)}\n"
        f"Clientes: {', '.join(CUSTOMERS)}\n"
        f"Contrasena de todas las cuentas: {password}\n"
        "Los administradores configuran su autenticador (TOTP) en el primer inicio de sesion.\n",
        encoding="utf-8",
    )

    workflow = TicketWorkflow(factory)
    queue = SupportTicketService(factory, SecurityAuditWriter())
    agent = StaffSessionUser(id=agent_ids[0], email=AGENTS[0], role="support_agent")
    for state, subject, customer_id in (
        ("open", "Ticket abierto para reclamar", customer_ids[0]),
        ("escalated", "No puedo recuperar mi acceso", customer_ids[0]),
        ("escalated", "Cobro duplicado en mi cuenta", customer_ids[1]),
        ("escalated", "Solicitud de cierre de cuenta", customer_ids[1]),
        ("resolved", "Caso resuelto de ejemplo", customer_ids[0]),
    ):
        ticket = workflow.create(
            customer_id,
            TicketCreate(
                subject=subject,
                category="account",
                description="Consulta ficticia para revisar la administracion.",
            ),
            "admin-demo",
            staff=False,
        )
        if state == "open":
            continue
        queue.claim(agent, ticket.id, "admin-demo")
        workflow.message(
            customer_id, ticket.id, "Hola, necesito ayuda con mi cuenta.", "admin-demo", staff=False
        )
        workflow.message(
            agent_ids[0], ticket.id, "Estamos revisando tu solicitud.", "admin-demo", staff=True
        )
        workflow.transition(
            agent_ids[0],
            ticket.id,
            "Requiere verificacion administrativa de identidad"
            if state == "escalated"
            else "Consulta atendida; puedes seguir usando la aplicacion.",
            "admin-demo",
            escalate=state == "escalated",
        )
    with factory() as session:
        count = session.scalar(select(func.count()).select_from(SupportTicket))
    print(f"Administrator demo initialized ({count} tickets). Credentials: {CREDENTIALS}")


if __name__ == "__main__":
    seed()
