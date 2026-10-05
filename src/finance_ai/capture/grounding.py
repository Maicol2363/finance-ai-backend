"""Deterministic grounding of a model interpretation against the user's own data.

The model only suggests. This module decides: it maps names to the caller's real accounts and
catalogs, never invents one, reclassifies what is certainly a transfer, and asks a single short
question only when the text is genuinely ambiguous (HU-013).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Literal

from finance_ai.capture.context import AccountEntry, CaptureContext, CatalogEntry
from finance_ai.capture.interpretation import RawInterpretation
from finance_ai.capture.schemas import (
    CaptureIntent,
    Clarification,
    ClarificationOption,
    ProposalStatus,
    TextChoice,
)

ONE_TAP_CONFIDENCE = 0.8
MAX_OPTIONS = 8
_FUZZY_THRESHOLD = 0.82
_TIE_MARGIN = 0.02

# Wording that, on its own, means "money moved between my own accounts". When the model calls
# such a message a purchase or income we ask instead of silently recording spending.
_OWN_ACCOUNT_VERBS = re.compile(
    r"\b(retir[eé]|consign[eé]|traslad[eé]|transfer[ií]\s+(de|desde)|"
    r"transferencia\s+(de|desde|entre))\b",
    re.IGNORECASE,
)

_QUESTION_ACCOUNT = {
    CaptureIntent.EXPENSE: "¿Con qué cuenta pagaste?",
    CaptureIntent.INCOME: "¿En qué cuenta recibiste el dinero?",
    CaptureIntent.TRANSFER: "¿De qué cuenta salió el dinero?",
}


@dataclass
class DraftState:
    """The editable working copy of a proposal while it is being resolved."""

    date: dt.date
    intent: CaptureIntent | None = None
    amount: Decimal | None = None
    description: str | None = None
    account_id: int | None = None
    destination_account_id: int | None = None
    destination_amount: Decimal | None = None
    category_id: int | None = None
    income_source_id: int | None = None
    confidence: float = 0.0
    account_candidates: tuple[int, ...] = ()
    destination_candidates: tuple[int, ...] = ()
    stated_currency: str | None = None
    date_stated: bool = False
    intent_conflict: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    status: ProposalStatus
    one_tap: bool
    clarification: Clarification | None = None
    message: str | None = None
    currency_code: str | None = None


@dataclass(frozen=True)
class _Match:
    kind: Literal["none", "one", "many"]
    ids: tuple[int, ...] = ()


def normalize_name(value: str) -> str:
    """Case, accent and punctuation-insensitive form used to compare names."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stripped.casefold()).split())


def _match(
    query: str | None, entries: tuple[CatalogEntry, ...] | tuple[AccountEntry, ...]
) -> _Match:
    if not query:
        return _Match("none")
    needle = normalize_name(query)
    if not needle:
        return _Match("none")
    scored: list[tuple[float, int]] = []
    for entry in entries:
        name = normalize_name(entry.name)
        if not name:
            continue
        if needle == name:
            score = 1.0
        elif len(needle) >= 3 and len(name) >= 3 and (needle in name or name in needle):
            score = 0.9
        else:
            score = SequenceMatcher(None, needle, name).ratio()
        scored.append((score, entry.id))
    if not scored:
        return _Match("none")
    best = max(score for score, _ in scored)
    if best < _FUZZY_THRESHOLD:
        return _Match("none")
    top = tuple(entry_id for score, entry_id in scored if score >= best - _TIE_MARGIN)
    return _Match("one" if len(top) == 1 else "many", top)


def ground(
    raw: RawInterpretation,
    context: CaptureContext,
    *,
    today: dt.date,
    text: str,
    has_follow_ups: bool,
) -> DraftState:
    """Maps a model interpretation onto the caller's real accounts and catalogs."""
    state = DraftState(
        date=today,
        intent=CaptureIntent(raw.intent) if raw.intent != "unknown" else None,
        amount=raw.amount,
        description=raw.description,
        destination_amount=raw.destination_amount,
        confidence=raw.confidence,
        stated_currency=raw.currency,
    )

    if raw.date is not None:
        if raw.date > today:
            state.notes.append("No registramos fechas futuras: usé la fecha de hoy.")
        else:
            state.date = raw.date
            state.date_stated = True

    account = _match(raw.account, context.accounts)
    if account.kind == "one":
        state.account_id = account.ids[0]
    elif account.kind == "many":
        state.account_candidates = account.ids

    destination = _match(raw.destination_account, context.accounts)
    if destination.kind == "one":
        state.destination_account_id = destination.ids[0]
    elif destination.kind == "many":
        state.destination_candidates = destination.ids

    category = _match(raw.category, context.categories)
    if category.kind == "one":
        state.category_id = category.ids[0]
    source = _match(raw.income_source, context.income_sources)
    if source.kind == "one":
        state.income_source_id = source.ids[0]

    # Two different accounts of the caller are a transfer, whatever the model called it.
    if (
        state.intent in (CaptureIntent.EXPENSE, CaptureIntent.INCOME)
        and state.destination_account_id is not None
        and state.destination_account_id != state.account_id
    ):
        state.intent = CaptureIntent.TRANSFER
        state.notes.append("Lo tomé como transferencia entre tus cuentas, no como gasto.")
    elif (
        state.intent in (CaptureIntent.EXPENSE, CaptureIntent.INCOME)
        and not has_follow_ups
        and _OWN_ACCOUNT_VERBS.search(text)
    ):
        state.intent_conflict = True
    return state


def apply_choice(state: DraftState, choice: TextChoice, context: CaptureContext) -> None:
    """Applies a tapped clarification option without calling the model again."""
    if choice.field == "intent":
        state.intent = CaptureIntent(choice.value)
        state.intent_conflict = False
        state.confidence = max(state.confidence, ONE_TAP_CONFIDENCE)
        return
    if choice.field in ("account", "destination_account"):
        try:
            account_id = int(choice.value)
        except ValueError:
            return
        if context.account(account_id) is None:
            return
        if choice.field == "account":
            state.account_id = account_id
            state.account_candidates = ()
            state.stated_currency = None
        else:
            state.destination_account_id = account_id
            state.destination_candidates = ()


def _account_options(
    ids: list[int] | tuple[int, ...], context: CaptureContext
) -> list[ClarificationOption]:
    options: list[ClarificationOption] = []
    for account_id in ids:
        account = context.account(account_id)
        if account is not None:
            options.append(
                ClarificationOption(
                    value=str(account.id), label=account.name, detail=account.currency_code
                )
            )
    return options[:MAX_OPTIONS]


def _intent_clarification(message: str) -> Clarification:
    return Clarification(
        field="intent",
        question=message,
        options=[
            ClarificationOption(value=CaptureIntent.EXPENSE.value, label="Gasto"),
            ClarificationOption(value=CaptureIntent.INCOME.value, label="Ingreso"),
            ClarificationOption(value=CaptureIntent.TRANSFER.value, label="Transferencia"),
        ],
    )


def _needs(clarification: Clarification) -> Outcome:
    return Outcome(
        status=ProposalStatus.NEEDS_CLARIFICATION,
        one_tap=False,
        clarification=clarification,
    )


def finalize(state: DraftState, context: CaptureContext) -> Outcome:
    """Completes the state when it can, and otherwise returns the one question to ask."""
    if not context.accounts:
        return Outcome(
            status=ProposalStatus.UNRECOGNIZED,
            one_tap=False,
            message="Primero crea una cuenta para poder registrar movimientos.",
        )

    if state.intent is None:
        if state.amount is None and state.account_id is None:
            return Outcome(
                status=ProposalStatus.UNRECOGNIZED,
                one_tap=False,
                message=(
                    "No encontré un gasto, ingreso o transferencia en tu mensaje. "
                    "Cuéntame qué pasó, cuánto fue y con qué cuenta."
                ),
            )
        return _needs(_intent_clarification("¿Qué quieres registrar?"))
    if state.intent_conflict:
        return _needs(_intent_clarification("Parece un movimiento entre tus cuentas. ¿Qué fue?"))
    intent = state.intent

    if state.amount is None:
        return _needs(Clarification(field="amount", question="¿De cuánto fue?"))

    if state.account_id is None:
        candidates = list(state.account_candidates) or [a.id for a in context.accounts]
        if len(candidates) == 1:
            state.account_id = candidates[0]
        else:
            return _needs(
                Clarification(
                    field="account",
                    question=_QUESTION_ACCOUNT[intent],
                    options=_account_options(candidates, context),
                )
            )
    source = context.account(state.account_id)
    if source is None:
        state.account_id = None
        return finalize(state, context)

    if intent is not CaptureIntent.TRANSFER:
        state.destination_account_id = None
        state.destination_amount = None
        if state.stated_currency and state.stated_currency != source.currency_code:
            same_currency = [
                a.id for a in context.accounts if a.currency_code == state.stated_currency
            ]
            options = same_currency or [a.id for a in context.accounts if a.id != source.id]
            return _needs(
                Clarification(
                    field="account",
                    question=(
                        f"Mencionaste {state.stated_currency}, pero {source.name} maneja "
                        f"{source.currency_code}. ¿Con qué cuenta fue?"
                    ),
                    options=_account_options(options, context),
                )
            )
        return _ready(state, source.currency_code)

    # Transfer: it needs a different destination account.
    if state.destination_account_id == state.account_id:
        state.destination_account_id = None
    if state.destination_account_id is None:
        pool = [a.id for a in context.accounts if a.id != source.id]
        candidates = [i for i in state.destination_candidates if i != source.id] or pool
        if not pool:
            return Outcome(
                status=ProposalStatus.UNRECOGNIZED,
                one_tap=False,
                message="Para una transferencia necesitas al menos dos cuentas activas.",
            )
        if len(candidates) == 1:
            state.destination_account_id = candidates[0]
        else:
            return _needs(
                Clarification(
                    field="destination_account",
                    question="¿A qué cuenta llegó el dinero?",
                    options=_account_options(candidates, context),
                )
            )
    destination = context.account(state.destination_account_id)
    if destination is None:
        state.destination_account_id = None
        return finalize(state, context)

    if destination.currency_code == source.currency_code:
        state.destination_amount = state.amount
    elif state.destination_amount is None:
        return _needs(
            Clarification(
                field="destination_amount",
                question=(f"¿Cuánto llegó a {destination.name} en {destination.currency_code}?"),
            )
        )
    return _ready(state, source.currency_code)


def _ready(state: DraftState, currency_code: str) -> Outcome:
    one_tap = state.confidence >= ONE_TAP_CONFIDENCE
    if not one_tap:
        state.notes.append("Revisa los datos antes de confirmar.")
    return Outcome(
        status=ProposalStatus.READY,
        one_tap=one_tap,
        currency_code=currency_code,
    )
