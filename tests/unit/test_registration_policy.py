import pytest

from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.schemas import RegisterCommand
from finance_ai.core.errors import RegistrationValidationError


def command(
    *,
    email: str = "Person@Example.COM",
    password: str = "correct horse battery staple",
    confirmation: str | None = None,
) -> RegisterCommand:
    return RegisterCommand(
        email=email,
        password=password,
        password_confirmation=password if confirmation is None else confirmation,
    )


def test_normalizes_email_without_transforming_password() -> None:
    password = "  Long Password With Spaces  "

    result = RegistrationPolicy().validate(
        command(email="  Person@Example.COM ", password=password)
    )

    assert result.normalized_email == "person@example.com"
    assert result.password == password


@pytest.mark.parametrize(
    ("candidate", "field"),
    [
        (command(email="not-an-email"), "email"),
        (command(password="short"), "password"),
        (command(password="x" * 129), "password"),
        (command(confirmation="different password"), "password_confirmation"),
        (command(confirmation=""), "password_confirmation"),
    ],
)
def test_rejects_invalid_registration(candidate: RegisterCommand, field: str) -> None:
    with pytest.raises(RegistrationValidationError) as captured:
        RegistrationPolicy().validate(candidate)

    assert field in (captured.value.field_errors or {})


def test_reports_multiple_fields_in_one_response() -> None:
    with pytest.raises(RegistrationValidationError) as captured:
        RegistrationPolicy().validate(
            command(email="bad", password="tiny", confirmation="different")
        )

    assert set(captured.value.field_errors or {}) == {
        "email",
        "password",
        "password_confirmation",
    }
