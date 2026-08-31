"""Align UOW-02 check-constraint names with SQLAlchemy metadata."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_uow02_constraint_names"
down_revision: str | Sequence[str] | None = "0002_uow02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RENAMES = (
    (
        "renewable_credential",
        "ck_renewable_credential_ck_renewable_credential_renewab_3273",
        "ck_renewable_credential_renewable_hash_length",
    ),
    (
        "renewable_credential",
        "ck_renewable_credential_ck_renewable_credential_renewab_5810",
        "ck_renewable_credential_renewable_expiry_after_issue",
    ),
    (
        "renewable_credential",
        "ck_renewable_credential_ck_renewable_credential_replace_62f9",
        "ck_renewable_credential_replacement_requires_consumption",
    ),
    (
        "user_session",
        "ck_user_session_ck_user_session_access_hash_length",
        "ck_user_session_access_hash_length",
    ),
    (
        "user_session",
        "ck_user_session_ck_user_session_session_expiry_order",
        "ck_user_session_session_expiry_order",
    ),
    (
        "user_session",
        "ck_user_session_ck_user_session_revocation_pair",
        "ck_user_session_revocation_pair",
    ),
)


def upgrade() -> None:
    for table_name, old_name, new_name in RENAMES:
        op.execute(f'ALTER TABLE "{table_name}" RENAME CONSTRAINT "{old_name}" TO "{new_name}"')


def downgrade() -> None:
    for table_name, old_name, new_name in reversed(RENAMES):
        op.execute(f'ALTER TABLE "{table_name}" RENAME CONSTRAINT "{new_name}" TO "{old_name}"')
