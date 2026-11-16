from __future__ import annotations

from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from finance_ai.notifications.formatting import format_amount, format_percent, format_period
from finance_ai.notifications.rendering import (
    LOGO_CONTENT_ID,
    render_budget_alert,
    render_test_email,
    render_verification_code,
)

VOID = {"meta", "br", "img", "hr", "input", "link"}


class _Balance(HTMLParser):
    """Counts unclosed or stray tags so a broken template fails loudly."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with stack {self.stack[-3:]}")
            return
        self.stack.pop()


def assert_well_formed(html: str) -> None:
    parser = _Balance()
    parser.feed(html)
    assert parser.errors == []
    assert parser.stack == []
    assert "{{" not in html and "{%" not in html


def alert(**overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "budget_name": "Mercado",
        "period_start": date(2026, 11, 1),
        "period_end": date(2026, 11, 30),
        "threshold": 80,
        "spent": Decimal("480000"),
        "limit": Decimal("600000"),
        "currency_code": "COP",
        "show_amounts": True,
        "unsubscribe_url": None,
    }
    values.update(overrides)
    return render_budget_alert(**values)  # type: ignore[arg-type]


# --- formatting ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("480000"), "COP", "$ 480.000"),
        (Decimal("602500.50"), "COP", "$ 602.500,50"),
        (Decimal("1234.5"), "usd", "USD 1.234,50"),
        (Decimal("0.005"), "COP", "$ 0,01"),
        (Decimal("-1500"), "COP", "-$ 1.500"),
        (Decimal("0"), "COP", "$ 0"),
    ],
)
def test_amounts_follow_the_spanish_convention(
    amount: Decimal, currency: str, expected: str
) -> None:
    assert format_amount(amount, currency) == expected


def test_percentages_are_whole_numbers_with_a_space() -> None:
    assert format_percent(Decimal("79.5")) == "80 %"
    assert format_percent(Decimal("102.4")) == "102 %"


def test_periods_repeat_month_and_year_only_when_they_change() -> None:
    assert (
        format_period(date(2026, 11, 1), date(2026, 11, 30)) == "Del 1 al 30 de noviembre de 2026"
    )
    assert (
        format_period(date(2026, 10, 30), date(2026, 11, 5))
        == "Del 30 de octubre al 5 de noviembre de 2026"
    )
    assert (
        format_period(date(2026, 12, 20), date(2027, 1, 19))
        == "Del 20 de diciembre de 2026 al 19 de enero de 2027"
    )


# --- budget alert -------------------------------------------------------------------------


def test_the_eighty_percent_alert_is_branded_html_with_a_text_alternative() -> None:
    email = alert()

    assert email.subject == "Llevas el 80 % de tu presupuesto «Mercado»"
    assert_well_formed(email.html)
    assert '<html lang="es"' in email.html
    assert f"cid:{LOGO_CONTENT_ID}" in email.html and 'alt="Finance AI"' in email.html
    assert "Vas por el 80 % de tu presupuesto" in email.html
    assert "$ 480.000" in email.html and "$ 600.000" in email.html and "$ 120.000" in email.html
    assert "Disponible" in email.html and "#fff4da" in email.html  # amber banner
    assert "Del 1 al 30 de noviembre de 2026" in email.html
    assert "Mercado" in email.html and "color-scheme" in email.html
    assert "Gastado: $ 480.000" in email.text and "Disponible: $ 120.000" in email.text
    assert "<" not in email.text.replace("«", "").replace("»", "")
    assert len(email.inline_images) == 1
    assert email.inline_images[0].content[:8] == b"\x89PNG\r\n\x1a\n"


def test_reaching_the_limit_uses_the_danger_style_and_says_so() -> None:
    exact = alert(threshold=100, spent=Decimal("600000"))
    over = alert(threshold=100, spent=Decimal("602500"))

    assert exact.subject == "Alcanzaste el límite de tu presupuesto «Mercado»"
    assert "Llegaste al límite de tu presupuesto" in exact.html and "#ffefee" in exact.html
    assert over.subject == "Superaste el límite de tu presupuesto «Mercado»"
    assert "Excedido" in over.html and "$ 2.500" in over.html
    assert "Excedido: $ 2.500" in over.text
    for email in (exact, over):
        assert_well_formed(email.html)


def test_amounts_can_be_hidden_from_html_and_text() -> None:
    email = alert(show_amounts=False)

    for body in (email.html, email.text):
        assert "480.000" not in body and "600.000" not in body and "Gastado" not in body
    assert "80 %" in email.html and "80 %" in email.text


def test_values_are_escaped_and_the_subject_cannot_carry_a_newline() -> None:
    email = alert(budget_name="<script>alert(1)</script>\r\nBcc: x@example.com")

    assert "<script>alert" not in email.html
    assert "&lt;script&gt;" in email.html
    assert "\n" not in email.subject and "\r" not in email.subject
    assert_well_formed(email.html)


def test_a_very_long_budget_name_is_shortened_in_the_subject() -> None:
    email = alert(budget_name="Supermercado " * 30)

    assert len(email.subject) < 140 and "…" in email.subject


def test_the_unsubscribe_link_is_present_only_when_given() -> None:
    without = alert()
    with_link = alert(unsubscribe_url="https://api.example.com/public/unsubscribe?t=abc")

    assert "Cancelar suscripción" not in without.html
    assert 'href="https://api.example.com/public/unsubscribe?t=abc"' in with_link.html
    assert "https://api.example.com/public/unsubscribe?t=abc" in with_link.text


def test_a_zero_limit_does_not_divide_by_zero() -> None:
    email = alert(threshold=100, limit=Decimal("0"), spent=Decimal("10"))

    assert "Superaste" in email.subject


# --- verification and test emails ---------------------------------------------------------


def test_the_verification_email_shows_the_code_and_its_validity() -> None:
    email = render_verification_code(code="123456", minutes=15)

    assert email.subject == "Tu código de verificación de Finance AI"
    assert_well_formed(email.html)
    assert "123 456" in email.html and "15 minutos" in email.html
    assert "123 456" in email.text and "15 minutos" in email.text
    assert "ignora este mensaje" in email.html
    assert "Cancelar suscripción" not in email.html  # a code request is not a subscription


def test_the_test_email_proves_layout_and_branding() -> None:
    email = render_test_email(unsubscribe_url="https://api.example.com/public/unsubscribe?t=abc")

    assert email.subject == "Correo de prueba de Finance AI"
    assert_well_formed(email.html)
    assert f"cid:{LOGO_CONTENT_ID}" in email.html and "Finance AI" in email.html
    assert "80 %" in email.text
