"""ADM-01 account lookup and ADM-02 suspension/reactivation.

State changes call procedure P1 and append their SEC-02 event in the same transaction, so a
critical change can never succeed without audit evidence: if the event cannot be written the
whole change rolls back and the caller receives a safe 503.
"""

from __future__ import annotations

import re
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.admin.repository import AdminAccountRepository
from finance_ai.admin.schemas import MAX_BIGINT, AdminAccount
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

_EMAIL_SHAPE: Final = re.compile(r"^[^@\s]+@[^@\s]+$")
_NON_PRINTABLE: Final = re.compile(r"[\x00-\x1f\x7f]")
_AUDITED_DENIALS: Final = frozenset({"self_target", "last_admin"})


def normalize_lookup(email: str | None, account_id: int | None) -> tuple[str | None, int | None]:
    """Validate ADM-01 lookup input: exactly one exact key, bounded and well formed."""
    if (email is None) == (account_id is None):
        raise AdminRequestInvalidError(
            {"lookup": ["Provide exactly one of an email address or an account ID."]}
        )
    if account_id is not None:
        if not 1 <= account_id <= MAX_BIGINT:
            raise AdminRequestInvalidError({"id": ["Enter a valid account ID."]})
        return None, account_id
    assert email is not None
    candidate = email.strip().lower()
    if (
        not 3 <= len(candidate) <= 320
        or _EMAIL_SHAPE.fullmatch(candidate) is None
        or _NON_PRINTABLE.search(candidate)
    ):
        raise AdminRequestInvalidError({"email": ["Enter a complete, valid email address."]})
    return candidate, None


class AdminAccountService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        audit_writer: SecurityAuditWriter,
        *,
        repository_factory: type[AdminAccountRepository] = AdminAccountRepository,
    ) -> None:
        self._factory = session_factory
        self._audit = audit_writer
        self._repository = repository_factory

    def lookup(self, *, email: str | None, account_id: int | None) -> AdminAccount:
        key_email, key_id = normalize_lookup(email, account_id)
        with self._factory() as session:
            account = self._repository(session).find(email=key_email, user_id=key_id)
        if account is None:
            raise AdminAccountNotFoundError()
        return account

    def detail(self, account_id: int) -> AdminAccount:
        return self.lookup(email=None, account_id=account_id)

    def suspend(
        self, *, actor_id: int, target_id: int, reason: str, correlation_id: str
    ) -> AdminAccount:
        return self._set_state(
            actor_id=actor_id,
            target_id=target_id,
            suspend=True,
            reason=reason,
            correlation_id=correlation_id,
        )

    def reactivate(self, *, actor_id: int, target_id: int, correlation_id: str) -> AdminAccount:
        return self._set_state(
            actor_id=actor_id,
            target_id=target_id,
            suspend=False,
            reason=None,
            correlation_id=correlation_id,
        )

    def _set_state(
        self,
        *,
        actor_id: int,
        target_id: int,
        suspend: bool,
        reason: str | None,
        correlation_id: str,
    ) -> AdminAccount:
        event_type = "account.suspended" if suspend else "account.reactivated"
        outcome = ""
        account: AdminAccount | None = None
        try:
            with self._factory() as session, session.begin():
                repository = self._repository(session)
                outcome = repository.set_suspension(
                    actor_id=actor_id, target_id=target_id, suspend=suspend, reason=reason
                )
                if outcome == "ok" or outcome in _AUDITED_DENIALS:
                    self._audit.append(
                        session,
                        event_type=event_type,
                        actor_id=str(actor_id),
                        target_id=str(target_id),
                        target_type="account",
                        result="success" if outcome == "ok" else "denied",
                        correlation_id=correlation_id,
                        context={"reason": "administrator_action" if outcome == "ok" else outcome},
                    )
                if outcome == "ok":
                    account = repository.find(user_id=target_id)
        except Exception as exc:
            raise AuditUnavailableError() from exc
        return self._result(outcome, account)

    @staticmethod
    def _result(outcome: str, account: AdminAccount | None) -> AdminAccount:
        if outcome == "ok" and account is not None:
            return account
        if outcome == "self_target":
            raise AdminSelfActionError()
        if outcome == "last_admin":
            raise LastActiveAdminError()
        if outcome == "not_found":
            raise AdminAccountNotFoundError()
        if outcome == "already_in_state":
            raise AccountStateConflictError()
        if outcome == "invalid_reason":
            raise AdminRequestInvalidError({"reason": ["Enter a reason of 1 to 500 characters."]})
        raise StaffAccessDeniedError()
