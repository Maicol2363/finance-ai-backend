from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from finance_ai.core.config import get_settings
from finance_ai.db.base import Base
from finance_ai.db.models import (
    Account,
    AccountMovement,
    AppUser,
    Category,
    Currency,
    FinancialTransaction,
    IncomeSource,
    ReceiptProposalModel,
    RenewableCredential,
    TextCaptureProposalModel,
    UserSession,
)

_MODELS = (
    Account,
    AccountMovement,
    AppUser,
    Category,
    Currency,
    FinancialTransaction,
    IncomeSource,
    ReceiptProposalModel,
    TextCaptureProposalModel,
    RenewableCredential,
    UserSession,
)
config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence every application logger
    # already created in this process (for example when a test or an embedded runner applies
    # migrations in-process).
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
