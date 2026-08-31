from collections.abc import Callable

from sqlalchemy.exc import IntegrityError

from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.schemas import RegisterCommand, RegistrationResult
from finance_ai.auth.uow import RegistrationUnitOfWork
from finance_ai.core.errors import EmailAlreadyRegisteredError
from finance_ai.db.seeds import REGISTRATION_CATALOG_TEMPLATE, RegistrationCatalogTemplate

UnitOfWorkFactory = Callable[[], RegistrationUnitOfWork]


class RegistrationService:
    def __init__(
        self,
        *,
        policy: RegistrationPolicy,
        unsafe_passwords: UnsafePasswordProvider,
        password_hasher: PasswordHasher,
        uow_factory: UnitOfWorkFactory,
        catalog_template: RegistrationCatalogTemplate = REGISTRATION_CATALOG_TEMPLATE,
    ) -> None:
        self.policy = policy
        self.unsafe_passwords = unsafe_passwords
        self.password_hasher = password_hasher
        self.uow_factory = uow_factory
        self.catalog_template = catalog_template

    def register(self, command: RegisterCommand) -> RegistrationResult:
        validated = self.policy.validate(command)
        self.unsafe_passwords.reject_if_unsafe(validated.password)
        password_hash = self.password_hasher.hash(validated.password)

        try:
            with self.uow_factory() as uow:
                currency = uow.currencies.require_active_by_code("COP")
                if uow.users.exists_by_email(validated.normalized_email):
                    raise EmailAlreadyRegisteredError
                user = uow.users.add(
                    email=validated.normalized_email,
                    password_hash=password_hash,
                    preferred_currency_id=currency.id,
                )
                uow.catalogs.add_defaults(user_id=user.id, template=self.catalog_template)
                uow.flush()
                uow.commit()
                return RegistrationResult(
                    user_id=user.id,
                    normalized_email=user.email,
                    preferred_currency=currency.code,
                )
        except IntegrityError as exc:
            if _is_email_unique_violation(exc):
                raise EmailAlreadyRegisteredError from exc
            raise


def _is_email_unique_violation(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return constraint_name in {"uq_app_user_email", "app_user_email_key"}
