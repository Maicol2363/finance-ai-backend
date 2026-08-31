from email_validator import EmailNotValidError, validate_email

from finance_ai.auth.schemas import RegisterCommand, ValidatedRegistration
from finance_ai.core.errors import RegistrationValidationError


class RegistrationPolicy:
    MIN_PASSWORD_LENGTH = 8
    MAX_PASSWORD_LENGTH = 128

    def validate(self, command: RegisterCommand) -> ValidatedRegistration:
        field_errors: dict[str, list[str]] = {}
        normalized_email = self._normalize_email(command.email, field_errors)
        self._validate_password(command.password, field_errors)
        self._validate_confirmation(command, field_errors)
        if field_errors:
            raise RegistrationValidationError(field_errors)
        return ValidatedRegistration(
            normalized_email=normalized_email,
            password=command.password,
        )

    @staticmethod
    def _normalize_email(raw_email: str, field_errors: dict[str, list[str]]) -> str:
        candidate = raw_email.strip().lower()
        if not candidate:
            field_errors.setdefault("email", []).append("Email is required.")
            return candidate
        if len(candidate) > 320:
            field_errors.setdefault("email", []).append("Email must be 320 characters or fewer.")
            return candidate
        try:
            validated = validate_email(candidate, check_deliverability=False)
        except EmailNotValidError:
            field_errors.setdefault("email", []).append("Enter a valid email address.")
            return candidate
        return validated.normalized.lower()

    @classmethod
    def _validate_password(cls, password: str, field_errors: dict[str, list[str]]) -> None:
        length = len(password)
        if length < cls.MIN_PASSWORD_LENGTH:
            field_errors.setdefault("password", []).append(
                "Password must be at least 8 characters."
            )
        elif length > cls.MAX_PASSWORD_LENGTH:
            field_errors.setdefault("password", []).append(
                "Password must be 128 characters or fewer."
            )

    @staticmethod
    def _validate_confirmation(
        command: RegisterCommand, field_errors: dict[str, list[str]]
    ) -> None:
        if not command.password_confirmation:
            field_errors.setdefault("password_confirmation", []).append(
                "Password confirmation is required."
            )
        elif command.password != command.password_confirmation:
            field_errors.setdefault("password_confirmation", []).append("Passwords do not match.")
