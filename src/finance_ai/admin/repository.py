"""Administrator persistence: allowlisted projections and calls to the database routines.

Repositories stage persistence in the caller's transaction and never commit.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finance_ai.admin.schemas import AdminAccount, SuspensionRecord
from finance_ai.db.models.admin import AccountSuspension
from finance_ai.db.models.identity import AppUser, Role

# Defence in depth for ADM-01 #3: a single exact match is expected; never more than this.
MAX_LOOKUP_ROWS = 25

_SET_SUSPENSION = text(
    "CALL sp_admin_set_account_suspension("
    "CAST(:actor AS bigint), CAST(:target AS bigint), CAST(:suspend AS boolean), "
    "CAST(:reason AS text), CAST(NULL AS text))"
)


class AdminAccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, *, email: str | None = None, user_id: int | None = None) -> AdminAccount | None:
        """Exact email or ID match only: no LIKE, prefix search or listing."""
        if (email is None) == (user_id is None):
            raise ValueError("Exactly one lookup key is required")
        condition = AppUser.email == email if email is not None else AppUser.id == user_id
        rows = (
            self._session.execute(
                select(
                    AppUser.id,
                    AppUser.email,
                    Role.name,
                    AppUser.is_active,
                    AppUser.created_at,
                )
                .join(Role, Role.id == AppUser.role_id)
                .where(condition)
                .limit(MAX_LOOKUP_ROWS)
            )
            .tuples()
            .all()
        )
        if len(rows) != 1:
            return None
        account_id, account_email, role_name, is_active, created_at = rows[0]
        suspension = None
        if not is_active:
            suspension = self._open_suspension(account_id)
        return AdminAccount(
            id=account_id,
            email=account_email,
            role=role_name,
            status="active" if is_active else "suspended",
            registered_at=created_at,
            suspension=suspension,
        )

    def set_suspension(
        self, *, actor_id: int, target_id: int, suspend: bool, reason: str | None
    ) -> str:
        """Run procedure P1 and return its result code."""
        result = self._session.execute(
            _SET_SUSPENSION,
            {"actor": actor_id, "target": target_id, "suspend": suspend, "reason": reason},
        ).scalar_one()
        return str(result)

    def _open_suspension(self, user_id: int) -> SuspensionRecord | None:
        row = self._session.execute(
            select(
                AccountSuspension.reason,
                AccountSuspension.suspended_at,
                AccountSuspension.suspended_by_user_id,
            )
            .where(AccountSuspension.user_id == user_id, AccountSuspension.reactivated_at.is_(None))
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        return SuspensionRecord(
            reason=row.reason,
            suspended_at=row.suspended_at,
            suspended_by_user_id=row.suspended_by_user_id,
        )
