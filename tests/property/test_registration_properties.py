import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.schemas import RegisterCommand, RegistrationResponse, RegistrationResult
from finance_ai.core.errors import RegistrationValidationError
from finance_ai.db.seeds import REGISTRATION_CATALOG_TEMPLATE

safe_passwords = st.text(
    alphabet=string.ascii_letters + string.digits + " !@#$%^&*()-_=+",
    min_size=8,
    max_size=128,
)


@given(
    local=st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=30),
    domain=st.text(alphabet=string.ascii_lowercase, min_size=2, max_size=20),
    password=safe_passwords,
)
def test_normalization_is_idempotent_and_password_is_exact(
    local: str, domain: str, password: str
) -> None:
    email = f"{local}@{domain}.com"
    policy = RegistrationPolicy()

    first = policy.validate(
        RegisterCommand(
            email=f"  {email.upper()}  ",
            password=password,
            password_confirmation=password,
        )
    )
    second = policy.validate(
        RegisterCommand(
            email=first.normalized_email,
            password=password,
            password_confirmation=password,
        )
    )

    assert first.normalized_email == second.normalized_email == email
    assert first.password == second.password == password


@given(
    length=st.one_of(
        st.integers(min_value=0, max_value=7), st.integers(min_value=129, max_value=180)
    )
)
def test_password_lengths_outside_the_contract_are_rejected(length: int) -> None:
    password = "x" * length

    with pytest.raises(RegistrationValidationError) as captured:
        RegistrationPolicy().validate(
            RegisterCommand(
                email="person@example.com",
                password=password,
                password_confirmation=password,
            )
        )

    assert "password" in (captured.value.field_errors or {})


@given(password=safe_passwords)
def test_public_response_can_never_echo_the_supplied_password(password: str) -> None:
    response = RegistrationResponse.from_result(
        RegistrationResult(
            user_id=1,
            normalized_email="person@example.com",
            preferred_currency="COP",
        )
    ).model_dump_json()

    assert password not in response
    assert "password" not in response.casefold()


def test_starter_catalog_template_has_exact_stable_invariants() -> None:
    template = REGISTRATION_CATALOG_TEMPLATE

    assert template.categories == ("Needs", "Wants", "Other")
    assert template.payment_methods == ("Cash", "Card", "Transfer")
    assert template.income_sources == ("Salary", "Other")
    assert len(set(template.categories)) == 3
    assert len(set(template.payment_methods)) == 3
    assert len(set(template.income_sources)) == 2
