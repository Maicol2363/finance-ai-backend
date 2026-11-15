"""Fictional Security Auditor fixture; never run against an ordinary or production database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex, token_urlsafe
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from finance_ai.auth.dependencies import get_password_hasher
from finance_ai.core.config import get_settings
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.session import get_session_factory

DEMO_DATABASE = "audit_demo_test"
CREDENTIALS = Path("demo-data/credentials.txt")
# (days ago, hour UTC, event type, outcome, actor key, target type, target key, reason)
_HISTORY: tuple[tuple[int, int, str, str, str | None, str | None, str | None, str | None], ...] = (
    (20, 13, "staff.login", "success", "agent", None, None, None),
    (20, 14, "support.ticket_claimed", "success", "agent", "support_ticket", "1", None),
    (18, 15, "staff.login_denied", "denied", None, None, None, "invalid_credentials"),
    (18, 15, "staff.login_denied", "denied", None, None, None, "invalid_credentials"),
    (18, 16, "staff.login", "success", "agent", None, None, None),
    (15, 12, "auth.mfa_failed", "failure", "auditor", "account", "auditor", "invalid_code"),
    (15, 12, "auth.mfa_success", "success", "auditor", "account", "auditor", None),
    (15, 12, "auth.login_success", "success", "auditor", "staff_session", "s1", None),
    (12, 19, "access.denied", "denied", "agent", "protected_resource", "security_audit", None),
    (9, 3, "staff.login_denied", "denied", "former", None, None, "ineligible"),
    (7, 14, "ticket.escalated", "success", "agent", "support_ticket", "2", None),
    (4, 17, "staff.login", "success", "agent", None, None, None),
    (2, 22, "staff.logout", "success", "agent", "staff_session", "s2", None),
    (1, 4, "staff.login_denied", "denied", None, None, None, "invalid_credentials"),
)


def seed() -> None:
    settings = get_settings()
    if (
        settings.environment not in {"local", "test"}
        or make_url(settings.database_url).database != DEMO_DATABASE
    ):
        raise SystemExit(
            f"Audit demo requires local/test and the isolated {DEMO_DATABASE} database"
        )
    factory = get_session_factory()
    with factory.begin() as session:
        if session.scalar(select(AppUser.id).limit(1)) is not None:
            events = session.scalar(select(func.count()).select_from(SecurityAuditEvent)) or 0
            if events >= len(_HISTORY) and CREDENTIALS.exists():
                return
            raise SystemExit("Incomplete audit demo; reset only its isolated volumes as documented")
        roles = dict(session.execute(select(Role.name, Role.id)).all())
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency is not None
        password = token_urlsafe(24)
        hashed = get_password_hasher().hash(password)
        accounts: dict[str, AppUser] = {}
        for key, email, role, active in (
            ("admin", "provisionador-inactivo@example.test", "admin", False),
            ("auditor", "auditor@example.test", "security_auditor", True),
            ("agent", "agente@example.test", "support_agent", True),
            ("customer", "cliente@example.test", "user", True),
            ("former", "ex-auditor@example.test", "security_auditor", False),
        ):
            account = AppUser(
                email=email,
                password_hash=hashed,
                role_id=roles[role],
                preferred_currency_id=currency,
                is_active=active,
            )
            session.add(account)
            session.flush()
            accounts[key] = account
        now = datetime.now(UTC)
        for key in ("auditor", "agent", "former"):
            staff = accounts[key]
            session.add(
                StaffInvitation(
                    email=staff.email,
                    role_id=staff.role_id,
                    token_hash=token_hex(32),
                    created_by_admin_user_id=accounts["admin"].id,
                    created_at=now - timedelta(days=30),
                    expires_at=now - timedelta(days=29),
                    consumed_at=now - timedelta(days=30),
                    activated_user_id=staff.id,
                )
            )
        day_start = now.replace(minute=0, second=0, microsecond=0)
        for days_ago, hour, event_type, outcome, actor, target_type, target, reason in _HISTORY:
            target_id: str | None = None
            if target in accounts:
                target_id = str(accounts[target].id)
            elif target_type == "protected_resource":
                target_id = target
            elif target is not None:
                target_id = f"demo-{target}"
            session.add(
                SecurityAuditEvent(
                    event_id=uuid4(),
                    event_type=event_type,
                    actor_id=str(accounts[actor].id) if actor is not None else None,
                    target_resource_type=target_type,
                    target_resource_id=target_id,
                    occurred_at=(day_start - timedelta(days=days_ago)).replace(hour=hour),
                    result=outcome,
                    correlation_id=f"audit-demo-{days_ago:02d}{hour:02d}",
                    context={"reason": reason} if reason is not None else {},
                )
            )
        CREDENTIALS.parent.mkdir(exist_ok=True)
        CREDENTIALS.touch(mode=0o600, exist_ok=True)
        CREDENTIALS.chmod(0o600)
        CREDENTIALS.write_text(
            "DATOS FICTICIOS LOCALES (base aislada audit_demo_test)\n"
            "Auditor de seguridad: auditor@example.test (requiere configurar TOTP al entrar)\n"
            "Agente de soporte: agente@example.test (no puede usar Auditoría)\n"
            "Cliente: cliente@example.test (solo app de clientes)\n"
            "Contraseña de las tres cuentas: " + password + "\n"
            "El administrador y el ex auditor están inactivos y no pueden iniciar sesión.\n"
        )
    print("Audit demo initialized. Local credentials: demo-data/credentials.txt")


if __name__ == "__main__":
    seed()
