from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from hypothesis import given, seed
from hypothesis import strategies as st

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.schemas import AccountResponse, AccountUpdateCommand
from finance_ai.core.errors import AccountDeleteBlockedError, AccountNotFoundError
from tests.unit.test_account_service import create, make_service

MAX_TEST_CENTS = 10**12


@dataclass(frozen=True)
class AccountNameCase:
    core: str
    left_padding: str
    right_padding: str

    @property
    def padded(self) -> str:
        return f"{self.left_padding}{self.core}{self.right_padding}"


@dataclass(frozen=True)
class MoneyCase:
    old_opening: Decimal
    new_opening: Decimal
    confirmed_delta: Decimal


@dataclass(frozen=True)
class OwnerPair:
    owner: int
    other_owner: int


@dataclass(frozen=True)
class LifecycleSequence:
    requested_states: tuple[bool, ...]


name_core = st.text(
    alphabet=st.characters(
        categories=("Ll", "Lu", "Nd"),
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=40,
)
account_name_cases = st.builds(
    AccountNameCase,
    core=name_core,
    left_padding=st.sampled_from(("", " ", "  ", "\u2003")),
    right_padding=st.sampled_from(("", " ", "  ", "\u2003")),
)
money_values = st.integers(min_value=-MAX_TEST_CENTS, max_value=MAX_TEST_CENTS).map(
    lambda cents: Decimal(cents) / 100
)
money_cases = st.builds(
    MoneyCase,
    old_opening=money_values,
    new_opening=money_values,
    confirmed_delta=money_values,
)
owner_pairs = st.builds(
    OwnerPair,
    owner=st.integers(min_value=1, max_value=10**9),
    other_owner=st.integers(min_value=1, max_value=10**9),
).filter(lambda pair: pair.owner != pair.other_owner)
lifecycle_sequences = st.builds(
    LifecycleSequence,
    requested_states=st.lists(st.booleans(), min_size=0, max_size=24).map(tuple),
)


@seed(20260907)
@given(case=account_name_cases)
def test_name_normalization_is_idempotent_and_whitespace_equivalent(
    case: AccountNameCase,
) -> None:
    policy = AccountPolicy()

    normalized = policy.normalize_name(case.padded)

    assert normalized == policy.normalize_name(normalized)
    assert normalized == case.core


@seed(20260907)
@given(case=money_cases)
def test_exact_money_round_trip_and_opening_correction_preserve_movement_delta(
    case: MoneyCase,
) -> None:
    service, store = make_service()
    account = create(service, opening=str(case.old_opening))
    store.accounts[account.id] = replace(
        store.accounts[account.id],
        balance=case.old_opening + case.confirmed_delta,
    )
    store.confirmed_accounts.add(account.id)

    updated = service.update(
        owner_id=7,
        account_id=account.id,
        command=AccountUpdateCommand(opening_balance=case.new_opening),
    )
    response = AccountResponse.from_result(updated)

    assert updated.balance == case.new_opening + case.confirmed_delta
    assert Decimal(response.opening_balance) == case.new_opening
    assert Decimal(response.balance) == updated.balance
    assert response.opening_balance.endswith(tuple(f"{digit}" for digit in range(10)))
    assert len(response.opening_balance.rsplit(".", 1)[-1]) == 2


@seed(20260907)
@given(sequence=lifecycle_sequences)
def test_status_transitions_match_a_simple_idempotent_model(
    sequence: LifecycleSequence,
) -> None:
    service, store = make_service()
    account = create(service)
    expected = True
    expected_mutations = 1

    for requested in sequence.requested_states:
        if requested != expected:
            expected_mutations += 1
        expected = requested
        observed = service.set_active(owner_id=7, account_id=account.id, is_active=requested)
        assert observed.is_active is expected

    assert store.accounts[account.id].is_active is expected
    assert store.commits == expected_mutations


@seed(20260907)
@given(pair=owner_pairs)
def test_owned_identifiers_never_cross_principal_boundaries(pair: OwnerPair) -> None:
    service, _ = make_service()
    account = create(service, owner_id=pair.owner)

    assert service.get(owner_id=pair.owner, account_id=account.id).id == account.id
    with pytest.raises(AccountNotFoundError) as captured:
        service.get(owner_id=pair.other_owner, account_id=account.id)

    assert captured.value.code == "account_not_found"


@seed(20260908)
@given(opening=money_values, has_movement=st.booleans())
def test_permanent_deletion_preserves_exactly_accounts_with_history(
    opening: Decimal,
    has_movement: bool,
) -> None:
    service, store = make_service()
    account = create(service, opening=str(opening))
    if has_movement:
        store.movement_accounts.add(account.id)

    if has_movement:
        with pytest.raises(AccountDeleteBlockedError):
            service.delete(owner_id=7, account_id=account.id)
        assert account.id in store.accounts
    else:
        service.delete(owner_id=7, account_id=account.id)
        assert account.id not in store.accounts
