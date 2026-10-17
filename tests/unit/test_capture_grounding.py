"""HU-013: grounding a model interpretation on the caller's own accounts and catalogs."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from finance_ai.capture.context import AccountEntry, CaptureContext
from finance_ai.capture.grounding import (
    DraftState,
    Outcome,
    apply_choice,
    extract_amount_hint,
    finalize,
    ground,
    has_oversized_amount,
    normalize_name,
)
from finance_ai.capture.interpretation import (
    TRANSFER_MONEY_MAX,
    RawInterpretation,
)
from finance_ai.capture.schemas import CaptureIntent, ProposalStatus, TextChoice
from tests.capture_support import ACCOUNTS, CONTEXT, raw

TODAY = dt.date(2026, 10, 2)


def resolve(
    fields: dict[str, object],
    *,
    text: str = "mensaje",
    context: CaptureContext = CONTEXT,
    has_follow_ups: bool = False,
) -> tuple[DraftState, Outcome]:
    interpretation: RawInterpretation = raw(**fields)
    state = ground(interpretation, context, today=TODAY, text=text, has_follow_ups=has_follow_ups)
    return state, finalize(state, context)


def test_complete_confident_expense_is_one_tap() -> None:
    state, outcome = resolve(
        {
            "intent": "expense",
            "amount": 15900,
            "description": "Almuerzo en Subway",
            "account": "Efectivo",
            "category": "Comida",
        }
    )

    assert outcome.status is ProposalStatus.READY
    assert outcome.one_tap is True
    assert outcome.clarification is None
    assert state.account_id == 1
    assert state.category_id == 10
    assert outcome.currency_code == "COP"


def test_low_confidence_is_ready_but_needs_a_careful_review() -> None:
    state, outcome = resolve(
        {
            "intent": "expense",
            "amount": 5000,
            "account": "Nequi",
            "description": "Bus",
            "confidence": 0.5,
        }
    )

    assert outcome.status is ProposalStatus.READY
    assert outcome.one_tap is False
    assert "Revisa los datos antes de confirmar." in state.notes


def test_two_owned_accounts_are_a_transfer_not_spending() -> None:
    state, outcome = resolve(
        {
            "intent": "expense",
            "amount": 200000,
            "account": "Nequi",
            "destination_account": "Bancolombia",
        }
    )

    assert state.intent is CaptureIntent.TRANSFER
    assert (state.account_id, state.destination_account_id) == (2, 3)
    assert outcome.status is ProposalStatus.READY
    assert any("transferencia" in note for note in state.notes)


def test_paying_a_person_that_is_not_an_account_stays_an_expense() -> None:
    state, outcome = resolve(
        {
            "intent": "expense",
            "amount": 50000,
            "account": "Nequi",
            "destination_account": "Juan Perez",
            "description": "Pago a Juan",
        }
    )

    assert state.intent is CaptureIntent.EXPENSE
    assert state.destination_account_id is None
    assert outcome.status is ProposalStatus.READY


def test_own_account_wording_never_becomes_spending_silently() -> None:
    _, outcome = resolve(
        {"intent": "expense", "amount": 100000, "account": "Bancolombia"},
        text="retiré 100 mil del cajero",
    )

    assert outcome.status is ProposalStatus.NEEDS_CLARIFICATION
    assert outcome.clarification is not None
    assert outcome.clarification.field == "intent"
    assert next(o.value for o in outcome.clarification.options) == "expense"
    assert "transfer" in [o.value for o in outcome.clarification.options]


def test_transfer_without_destination_asks_for_it_excluding_the_source() -> None:
    _, outcome = resolve({"intent": "transfer", "amount": 90000, "account": "Nequi"})

    assert outcome.clarification is not None
    assert outcome.clarification.field == "destination_account"
    values = [o.value for o in outcome.clarification.options]
    assert "2" not in values
    assert values == ["1", "3", "4"]


def test_transfer_between_only_two_accounts_infers_the_destination() -> None:
    context = replace(CONTEXT, accounts=ACCOUNTS[:2])
    state, outcome = resolve(
        {"intent": "transfer", "amount": 90000, "account": "Nequi"}, context=context
    )

    assert state.destination_account_id == 1
    assert outcome.status is ProposalStatus.READY


def test_missing_amount_asks_only_for_the_amount() -> None:
    _, outcome = resolve({"intent": "expense", "account": "Efectivo"})

    assert outcome.clarification is not None
    assert outcome.clarification.field == "amount"
    assert outcome.clarification.options == []


def test_unspecified_account_with_several_accounts_offers_them() -> None:
    _, outcome = resolve({"intent": "expense", "amount": 8000})

    assert outcome.clarification is not None
    assert outcome.clarification.field == "account"
    assert [o.label for o in outcome.clarification.options] == [
        "Efectivo",
        "Nequi",
        "Bancolombia",
        "Dólares",
    ]
    assert outcome.clarification.options[0].detail == "COP"


def test_a_single_active_account_is_used_without_asking() -> None:
    context = replace(CONTEXT, accounts=ACCOUNTS[:1])
    state, outcome = resolve(
        {"intent": "income", "amount": 1000000, "description": "Sueldo"}, context=context
    )

    assert state.account_id == 1
    assert outcome.status is ProposalStatus.READY


def test_invented_account_names_are_never_accepted() -> None:
    state, outcome = resolve({"intent": "expense", "amount": 8000, "account": "Banco Inventado"})

    assert state.account_id is None
    assert outcome.clarification is not None
    assert outcome.clarification.field == "account"


def test_names_match_ignoring_case_accents_and_small_typos() -> None:
    assert normalize_name("  Dólares  ") == "dolares"
    for spelled in ("bancolombia", "BANCOLOMBIA", "bancolonbia"):
        state, _ = resolve({"intent": "expense", "amount": 1000, "account": spelled})
        assert state.account_id == 3, spelled


def test_account_name_inside_a_longer_phrase_is_matched() -> None:
    state, _ = resolve({"intent": "expense", "amount": 1000, "account": "mi cuenta Nequi"})

    assert state.account_id == 2


def test_ambiguous_names_offer_only_the_candidates() -> None:
    context = replace(
        CONTEXT,
        accounts=(
            AccountEntry(id=5, name="Ahorros Davivienda", currency_code="COP"),
            AccountEntry(id=6, name="Ahorros Bancolombia", currency_code="COP"),
            AccountEntry(id=7, name="Efectivo", currency_code="COP"),
        ),
    )
    _, outcome = resolve(
        {"intent": "expense", "amount": 1000, "account": "ahorros"}, context=context
    )

    assert outcome.clarification is not None
    assert [o.value for o in outcome.clarification.options] == ["5", "6"]


def test_currency_that_the_account_cannot_hold_is_questioned() -> None:
    _, outcome = resolve(
        {"intent": "expense", "amount": 20, "currency": "usd", "account": "Efectivo"}
    )

    assert outcome.clarification is not None
    assert outcome.clarification.field == "account"
    assert [o.label for o in outcome.clarification.options] == ["Dólares"]
    assert "USD" in outcome.clarification.question


def test_cross_currency_transfer_needs_the_arrived_amount() -> None:
    state, outcome = resolve(
        {
            "intent": "transfer",
            "amount": 400000,
            "account": "Bancolombia",
            "destination_account": "Dólares",
        }
    )
    assert outcome.clarification is not None
    assert outcome.clarification.field == "destination_amount"

    state, outcome = resolve(
        {
            "intent": "transfer",
            "amount": 400000,
            "account": "Bancolombia",
            "destination_account": "Dólares",
            "destination_amount": 100,
        }
    )
    assert outcome.status is ProposalStatus.READY
    assert str(state.destination_amount) == "100.00"


def test_same_currency_transfer_moves_the_same_amount() -> None:
    state, outcome = resolve(
        {
            "intent": "transfer",
            "amount": 5000,
            "account": "Nequi",
            "destination_account": "Efectivo",
            "destination_amount": 999,
        }
    )

    assert outcome.status is ProposalStatus.READY
    assert state.destination_amount == state.amount


def test_future_dates_fall_back_to_today_with_a_note() -> None:
    state, _ = resolve(
        {"intent": "expense", "amount": 1000, "account": "Efectivo", "date": "2026-12-25"}
    )

    assert state.date == TODAY
    assert any("fechas futuras" in note for note in state.notes)


def test_stated_past_date_is_kept() -> None:
    state, _ = resolve(
        {"intent": "expense", "amount": 1000, "account": "Efectivo", "date": "2026-10-01"}
    )

    assert state.date == dt.date(2026, 10, 1)
    assert state.date_stated is True


def test_non_financial_message_is_unrecognized() -> None:
    _, outcome = resolve({"intent": "unknown", "confidence": 0.1})

    assert outcome.status is ProposalStatus.UNRECOGNIZED
    assert outcome.message


def test_unknown_intent_with_an_amount_asks_what_to_register() -> None:
    _, outcome = resolve({"intent": "unknown", "amount": 30000})

    assert outcome.clarification is not None
    assert outcome.clarification.field == "intent"
    assert [o.value for o in outcome.clarification.options] == ["expense", "income", "transfer"]


def test_without_accounts_the_user_is_sent_to_create_one() -> None:
    _, outcome = resolve(
        {"intent": "expense", "amount": 1000}, context=replace(CONTEXT, accounts=())
    )

    assert outcome.status is ProposalStatus.UNRECOGNIZED
    assert "cuenta" in (outcome.message or "")


def test_tapping_an_account_choice_completes_the_proposal_without_the_model() -> None:
    state, outcome = resolve({"intent": "expense", "amount": 8000, "description": "Bus"})
    assert outcome.clarification is not None

    apply_choice(state, TextChoice(field="account", value="2"), CONTEXT)

    assert state.account_id == 2
    assert finalize(state, CONTEXT).status is ProposalStatus.READY


def test_tapping_an_intent_choice_resolves_the_conflict_and_is_one_tap() -> None:
    state, _ = resolve(
        {"intent": "expense", "amount": 100000, "account": "Bancolombia", "confidence": 0.4},
        text="retiré 100 mil",
    )

    apply_choice(state, TextChoice(field="intent", value="transfer"), CONTEXT)
    outcome = finalize(state, CONTEXT)

    assert state.intent is CaptureIntent.TRANSFER
    assert outcome.clarification is not None
    assert outcome.clarification.field == "destination_account"


def test_choices_for_unknown_accounts_are_ignored() -> None:
    state, _ = resolve({"intent": "expense", "amount": 8000})

    apply_choice(state, TextChoice(field="account", value="999"), CONTEXT)
    apply_choice(state, TextChoice(field="account", value="abc"), CONTEXT)

    assert state.account_id is None


def test_oversized_amount_gets_an_explanation_not_a_generic_question() -> None:
    state, outcome = resolve(
        {"intent": "transfer"},
        text="transfiere 2000000000000000000000000000 pesos a nequi",
    )

    assert state.amount is None
    assert state.amount_too_large is True
    assert outcome.clarification.field == "amount"
    assert "demasiado grande" in outcome.clarification.question
    assert "9.999.999.999,99" in outcome.clarification.question


def test_oversized_destination_amount_is_explained() -> None:
    state, outcome = resolve(
        {
            "intent": "transfer",
            "amount": 50000,
            "account": "Bancolombia",
            "destination_account": "Dólares",
        },
        text="pasa 50000 de Bancolombia a Dólares, llegan 99999999999999999",
    )

    assert state.amount == Decimal("50000")
    assert outcome.clarification.field == "destination_amount"
    assert "demasiado grande" in outcome.clarification.question


def test_has_oversized_amount_ignores_normal_text() -> None:
    assert not has_oversized_amount("almorcé 15.900 en el Subway")
    assert has_oversized_amount("me fueron 99999999999")


def test_text_amount_hint_ignores_amounts_beyond_the_range() -> None:
    assert extract_amount_hint("gasté 15000 en el cine") is not None
    # An expense/income row tops at 9.999.999.999,99, a transfer reaches 16 digits.
    assert extract_amount_hint("gasté 20000000000 en el cine") is None
    assert (
        extract_amount_hint(
            "transfiere 20000000000000 pesos",
            ceiling=TRANSFER_MONEY_MAX,
        )
        is not None
    )
    assert (
        extract_amount_hint(
            "transfiere 200000000000000000 pesos",
            ceiling=TRANSFER_MONEY_MAX,
        )
        is None
    )


def test_transfer_message_allows_more_than_the_expense_limit() -> None:
    state, outcome = resolve(
        {"intent": "transfer"},
        text="transfiere 99999999999999 pesos entre mis cuentas",
    )

    assert state.amount == Decimal("99999999999999.00")
    assert outcome.clarification.field == "account"


def test_transfer_message_explains_its_own_ceiling() -> None:
    state, outcome = resolve(
        {"intent": "transfer"},
        text="transfiere 200000000000000000 pesos entre mis cuentas",
    )

    assert state.amount is None
    assert state.amount_too_large is True
    assert "9.999.999.999.999.999,99" in outcome.clarification.question


def test_model_output_is_tolerated_when_malformed() -> None:
    parsed = RawInterpretation.model_validate(
        {
            "intent": "EXPENSE ",
            "confidence": "high",
            "amount": "-5",
            "currency": "pesos",
            "date": "mañana",
            "description": "  Taxi   al aeropuerto ",
            "account": 42,
        }
    )

    assert parsed.intent == "expense"
    assert parsed.confidence == 0.0
    assert parsed.amount is None
    assert parsed.currency is None
    assert parsed.date is None
    assert parsed.description == "Taxi al aeropuerto"
    assert parsed.account is None


@pytest.mark.parametrize(
    ("absurd_number",),
    [
        ("1e600",),
        ("1E-600",),
        ("9" + "0" * 60,),
        ("900e300",),
    ],
)
def test_absurd_model_amounts_degrade_to_missing(absurd_number: str) -> None:
    parsed = RawInterpretation.model_validate(
        {"intent": "expense", "confidence": 0.9, "amount": absurd_number}
    )
    assert parsed.amount is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13 mil pesos y ya", "13000.00"),
        ("gasté 15.900 en Subway", "15900.00"),
        ("me pagaron 2 millones", "2000000.00"),
        ("20 lucas", "20000.00"),
        ("1,5 millones", "1500000.00"),
        ("compré 3 cosas", None),
        ("pagué 13 mil y 20 mil", None),
        ("hola", None),
    ],
)
def test_the_amount_is_recovered_from_the_text_only_when_unambiguous(
    text: str, expected: str | None
) -> None:
    hint = extract_amount_hint(text)

    assert (None if hint is None else format(hint, ".2f")) == expected


def test_a_message_with_only_an_amount_asks_what_to_register_instead_of_being_ignored() -> None:
    state, outcome = resolve({"intent": "unknown", "confidence": 0.1}, text="13 mil pesos y ya")

    assert str(state.amount) == "13000.00"
    assert outcome.status is ProposalStatus.NEEDS_CLARIFICATION
    assert outcome.clarification is not None
    assert outcome.clarification.field == "intent"
    assert any("monto" in note for note in state.notes)


def test_a_missing_concept_is_asked_once_with_a_way_to_skip() -> None:
    state, outcome = resolve({"intent": "expense", "amount": 13000, "account": "Efectivo"})

    assert outcome.status is ProposalStatus.NEEDS_CLARIFICATION
    assert outcome.clarification is not None
    assert outcome.clarification.field == "description"
    assert [o.value for o in outcome.clarification.options] == ["skip"]

    apply_choice(state, TextChoice(field="description", value="skip"), CONTEXT)
    assert finalize(state, CONTEXT).status is ProposalStatus.READY


def test_the_concept_question_is_not_repeated_after_it_was_asked() -> None:
    state, _ = resolve({"intent": "income", "amount": 500000, "account": "Nequi"})
    state.description_asked = True

    assert finalize(state, CONTEXT).status is ProposalStatus.READY


def test_a_category_counts_as_the_concept() -> None:
    _, outcome = resolve(
        {"intent": "expense", "amount": 13000, "account": "Efectivo", "category": "Comida"}
    )

    assert outcome.status is ProposalStatus.READY


def test_transfers_never_ask_for_a_concept() -> None:
    _, outcome = resolve(
        {
            "intent": "transfer",
            "amount": 5000,
            "account": "Nequi",
            "destination_account": "Efectivo",
        }
    )

    assert outcome.status is ProposalStatus.READY
