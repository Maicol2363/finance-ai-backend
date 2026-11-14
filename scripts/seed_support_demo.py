"""Fictional support fixture; never run against an ordinary or production database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex, token_urlsafe

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.dependencies import get_password_hasher
from finance_ai.core.config import get_settings
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.support import SupportTicket
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.schemas import TicketCreate
from finance_ai.support.service import SupportTicketService
from finance_ai.support.workflow import TicketWorkflow


def seed() -> None:
    settings = get_settings()
    if (
        settings.environment not in {"local", "test"}
        or make_url(settings.database_url).database != "support_demo_test"
    ):
        raise SystemExit(
            "Support demo requires local/test and the isolated support_demo_test database"
        )
    factory = get_session_factory()
    with factory.begin() as session:
        if session.scalar(select(AppUser.id).limit(1)) is not None:
            count = session.scalar(select(func.count()).select_from(SupportTicket))
            if count is not None and count >= 4 and Path("demo-data/credentials.txt").exists():
                return
            raise SystemExit(
                "Incomplete support demo; reset only its isolated volumes as documented"
            )
        roles = dict(session.execute(select(Role.name, Role.id)).all())
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency is not None
        password = token_urlsafe(24)
        hashed = get_password_hasher().hash(password)
        people = []
        for email, role, active in (
            ("cliente@example.test", "user", True),
            ("otro-cliente@example.test", "user", True),
            ("agente@example.test", "support_agent", True),
            ("otro-agente@example.test", "support_agent", True),
            ("provisionador-inactivo@example.test", "admin", False),
        ):
            user = AppUser(
                email=email,
                password_hash=hashed,
                role_id=roles[role],
                preferred_currency_id=currency,
                is_active=active,
            )
            session.add(user)
            session.flush()
            people.append(user)
        customer_id, agent_id = people[0].id, people[2].id
        now = datetime.now(UTC)
        for agent in people[2:4]:
            session.add(
                StaffInvitation(
                    email=agent.email,
                    role_id=agent.role_id,
                    token_hash=token_hex(32),
                    created_by_admin_user_id=people[4].id,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    consumed_at=now,
                    activated_user_id=agent.id,
                )
            )
        credentials = Path("demo-data/credentials.txt")
        credentials.parent.mkdir(exist_ok=True)
        credentials.touch(mode=0o600, exist_ok=True)
        credentials.chmod(0o600)
        credentials.write_text(
            "DATOS FICTICIOS LOCALES\n"
            "Cliente: cliente@example.test\nOtro cliente: otro-cliente@example.test\n"
            "Agente: agente@example.test\nOtro agente: otro-agente@example.test\n"
            "Contraseña de las cuatro cuentas: "
            + password
            + "\nNo existe un administrador habilitado para iniciar sesión.\n"
        )
    workflow = TicketWorkflow(factory)
    queue = SupportTicketService(factory, SecurityAuditWriter())
    agent = StaffSessionUser(id=agent_id, email="agente@example.test", role="support_agent")
    for state, subject in (
        ("open", "Ticket abierto para reclamar"),
        ("in_progress", "Conversación en curso"),
        ("escalated", "Caso escalado de ejemplo"),
        ("resolved", "Caso resuelto de ejemplo"),
    ):
        ticket = workflow.create(
            customer_id,
            TicketCreate(
                subject=subject,
                category="general",
                description="Consulta ficticia para pruebas de soporte.",
            ),
            "support-demo",
            staff=False,
        )
        if state == "open":
            continue
        queue.claim(agent, ticket.id, "support-demo")
        workflow.message(
            customer_id,
            ticket.id,
            "Hola, necesito ayuda con la aplicación.",
            "support-demo",
            staff=False,
        )
        workflow.message(
            agent_id, ticket.id, "Estamos revisando tu solicitud.", "support-demo", staff=True
        )
        if state in {"escalated", "resolved"}:
            workflow.transition(
                agent_id,
                ticket.id,
                "Revisión administrativa requerida"
                if state == "escalated"
                else "Consulta atendida; puedes continuar usando la aplicación.",
                "support-demo",
                escalate=state == "escalated",
            )
    print("Support demo initialized. Local credentials: demo-data/credentials.txt")


if __name__ == "__main__":
    seed()
