"""Spanish (es-CO) presentation of amounts and dates for emails. No locale libraries: the output
must not depend on what the server happens to have installed."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def format_amount(amount: Decimal, currency_code: str) -> str:
    """``$ 480.000`` for COP, ``USD 1.234,50`` for others; ``.00`` is dropped, a minus is kept."""
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    whole, _, cents = f"{abs(rounded):.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    number = grouped if cents == "00" else f"{grouped},{cents}"
    prefix = "$" if currency_code.upper() == "COP" else currency_code.upper()
    return f"{sign}{prefix} {number}"


def format_percent(percent: Decimal) -> str:
    """Whole percentages (``80 %``, ``102 %``); the space follows the Spanish convention."""
    whole = percent.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(whole)} %"


def format_period(start: date, end: date) -> str:
    """``Del 1 al 30 de noviembre de 2026``; month and year are repeated only when they change."""
    if (start.year, start.month) == (end.year, end.month):
        return f"Del {start.day} al {end.day} de {_MONTHS[end.month - 1]} de {end.year}"
    if start.year == end.year:
        return (
            f"Del {start.day} de {_MONTHS[start.month - 1]} "
            f"al {end.day} de {_MONTHS[end.month - 1]} de {end.year}"
        )
    return (
        f"Del {start.day} de {_MONTHS[start.month - 1]} de {start.year} "
        f"al {end.day} de {_MONTHS[end.month - 1]} de {end.year}"
    )
