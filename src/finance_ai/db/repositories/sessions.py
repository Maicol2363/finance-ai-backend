from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from finance_ai.auth.session_repositories import (
    AuthUserRecord,
    RenewableRecord,
    SessionRecord,
)
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession


class SQLAlchemySessionUserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_auth_by_email(self, normalized_email: str) -> AuthUserRecord | None:
        row = self.session.execute(
            select(AppUser, Currency.code)
            .join(Currency, Currency.id == AppUser.preferred_currency_id)
            .where(AppUser.email == normalized_email)
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        user, currency_code = row
        return AuthUserRecord(
            id=user.id,
            email=user.email,
            password_hash=user.password_hash,
            preferred_currency=currency_code,
            is_active=user.is_active,
        )

    def lock_by_id(self, user_id: int) -> AuthUserRecord | None:
        row = self.session.execute(
            select(AppUser, Currency.code)
            .join(Currency, Currency.id == AppUser.preferred_currency_id)
            .where(AppUser.id == user_id)
            .with_for_update(of=AppUser)
        ).one_or_none()
        if row is None:
            return None
        user, currency_code = row
        return AuthUserRecord(
            id=user.id,
            email=user.email,
            password_hash=user.password_hash,
            preferred_currency=currency_code,
            is_active=user.is_active,
        )

    def update_password_hash(self, *, user_id: int, password_hash: str) -> None:
        self.session.execute(
            update(AppUser)
            .where(AppUser.id == user_id)
            .values(password_hash=password_hash, updated_at=func.now())
        )


class SQLAlchemySessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active_session_ids_for_user(self, *, user_id: int, now: datetime) -> list[int]:
        return list(
            self.session.scalars(
                select(UserSession.id)
                .where(
                    UserSession.user_id == user_id,
                    UserSession.revoked_at.is_(None),
                    UserSession.idle_expires_at > now,
                    UserSession.absolute_expires_at > now,
                )
                .order_by(UserSession.created_at, UserSession.id)
                .with_for_update()
            )
        )

    def add_session(
        self,
        *,
        user_id: int,
        platform: str,
        access_token_hash: str,
        access_expires_at: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        now: datetime,
    ) -> int:
        session = UserSession(
            user_id=user_id,
            platform=platform,
            access_token_hash=access_token_hash,
            access_expires_at=access_expires_at,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            last_renewed_at=now,
        )
        self.session.add(session)
        self.session.flush()
        return session.id

    def add_renewable(
        self,
        *,
        session_id: int,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> int:
        renewable = RenewableCredential(
            session_id=session_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        self.session.add(renewable)
        self.session.flush()
        return renewable.id

    def resolve_session_id_by_renewable_hash(self, token_hash: str) -> int | None:
        return self.session.scalar(
            select(RenewableCredential.session_id)
            .where(RenewableCredential.token_hash == token_hash)
            .limit(1)
        )

    def lock_session(self, session_id: int) -> SessionRecord | None:
        row = self.session.execute(
            select(UserSession, AppUser, Currency.code)
            .join(AppUser, AppUser.id == UserSession.user_id)
            .join(Currency, Currency.id == AppUser.preferred_currency_id)
            .where(UserSession.id == session_id)
            .with_for_update(of=UserSession)
        ).one_or_none()
        if row is None:
            return None
        session, user, currency_code = row
        return SessionRecord(
            id=session.id,
            user=AuthUserRecord(
                id=user.id,
                email=user.email,
                password_hash=user.password_hash,
                preferred_currency=currency_code,
                is_active=user.is_active,
            ),
            access_expires_at=session.access_expires_at,
            idle_expires_at=session.idle_expires_at,
            absolute_expires_at=session.absolute_expires_at,
            revoked_at=session.revoked_at,
        )

    def lock_renewable_by_hash(self, token_hash: str) -> RenewableRecord | None:
        renewable = self.session.scalar(
            select(RenewableCredential)
            .where(RenewableCredential.token_hash == token_hash)
            .with_for_update()
        )
        if renewable is None:
            return None
        return RenewableRecord(
            id=renewable.id,
            session_id=renewable.session_id,
            expires_at=renewable.expires_at,
            consumed_at=renewable.consumed_at,
        )

    def rotate(
        self,
        *,
        session_id: int,
        previous_renewable_id: int,
        replacement_renewable_id: int,
        access_token_hash: str,
        access_expires_at: datetime,
        idle_expires_at: datetime,
        now: datetime,
    ) -> None:
        self.session.execute(
            update(RenewableCredential)
            .where(RenewableCredential.id == previous_renewable_id)
            .values(consumed_at=now, replaced_by_id=replacement_renewable_id)
        )
        self.session.execute(
            update(UserSession)
            .where(UserSession.id == session_id)
            .values(
                access_token_hash=access_token_hash,
                access_expires_at=access_expires_at,
                idle_expires_at=idle_expires_at,
                last_renewed_at=now,
            )
        )

    def revoke(self, *, session_id: int, now: datetime, reason: str) -> None:
        self.session.execute(
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now, revocation_reason=reason)
        )

    def find_principal_by_access_hash(
        self, *, access_token_hash: str, now: datetime
    ) -> PublicAuthenticatedUser | None:
        row = self.session.execute(
            select(AppUser.id, AppUser.email, Currency.code)
            .join(UserSession, UserSession.user_id == AppUser.id)
            .join(Currency, Currency.id == AppUser.preferred_currency_id)
            .where(
                UserSession.access_token_hash == access_token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.access_expires_at > now,
                UserSession.idle_expires_at > now,
                UserSession.absolute_expires_at > now,
                AppUser.is_active.is_(True),
                Currency.is_active.is_(True),
            )
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        user_id, email, currency_code = row
        return PublicAuthenticatedUser(
            user_id=user_id,
            email=email,
            preferred_currency=currency_code,
        )

    def cleanup_terminal(self, *, cutoff: datetime, now: datetime, batch_size: int) -> int:
        session_ids = list(
            self.session.scalars(
                select(UserSession.id)
                .where(
                    or_(
                        UserSession.revoked_at < cutoff,
                        UserSession.absolute_expires_at < cutoff,
                    ),
                    or_(
                        UserSession.revoked_at.is_not(None),
                        UserSession.absolute_expires_at <= now,
                    ),
                )
                .order_by(UserSession.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        if not session_ids:
            return 0
        self.session.execute(delete(UserSession).where(UserSession.id.in_(session_ids)))
        return len(session_ids)
