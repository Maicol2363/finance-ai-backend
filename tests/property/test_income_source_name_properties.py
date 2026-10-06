import pytest
from hypothesis import given
from hypothesis import strategies as st

from finance_ai.core.errors import IncomeSourceValidationError
from finance_ai.income_sources.service import MAX_NAME_LENGTH, _normalize_name


@given(st.text(max_size=200))
def test_normalize_name_is_trimmed_or_rejected(value: str) -> None:
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_NAME_LENGTH:
        with pytest.raises(IncomeSourceValidationError):
            _normalize_name(value)
    else:
        assert _normalize_name(value) == stripped


@given(st.text(min_size=1, max_size=MAX_NAME_LENGTH))
def test_normalize_name_is_idempotent(value: str) -> None:
    try:
        first = _normalize_name(value)
    except IncomeSourceValidationError:
        return
    assert _normalize_name(first) == first


@given(st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1))
def test_ascii_names_are_case_foldable_for_uniqueness(value: str) -> None:
    normalized = _normalize_name(value)
    assert normalized == value.strip()
    assert normalized.lower() == normalized.lower()
