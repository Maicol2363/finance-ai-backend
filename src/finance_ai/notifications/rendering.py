"""Turns a notification into a subject, an HTML body and a plain-text body.

Every value reaches the HTML through Jinja2 autoescape; templates never build markup from
user-provided text. The plain-text version is always produced next to the HTML one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from finance_ai.notifications.formatting import format_amount, format_percent, format_period
from finance_ai.notifications.messages import InlineImage

APP_NAME = "Finance AI"
LOGO_CONTENT_ID = "finance-ai-logo"
_WARNING = ("#fff4da", "#8a6900", "#c98a00")
_DANGER = ("#ffefee", "#b42318", "#b42318")
_OK_COLOR = "#236b52"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html: str
    text: str
    inline_images: tuple[InlineImage, ...]


@lru_cache
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("finance_ai.notifications", "templates"),
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default=False),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@lru_cache
def load_logo() -> InlineImage:
    data = (resources.files("finance_ai.notifications") / "assets" / "logo.png").read_bytes()
    return InlineImage(content_id=LOGO_CONTENT_ID, content=data)


def _render(
    template: str, *, subject: str, preheader: str, footer_reason: str, **context: Any
) -> RenderedEmail:
    environment = _environment()
    shared: dict[str, Any] = {
        "app_name": APP_NAME,
        "logo_cid": LOGO_CONTENT_ID,
        "title": subject,
        "preheader": preheader,
        "footer_reason": footer_reason,
        "unsubscribe_url": None,
        **context,
    }
    return RenderedEmail(
        subject=subject,
        html=environment.get_template(f"{template}.html.j2").render(**shared),
        text=environment.get_template(f"{template}.txt.j2").render(**shared),
        inline_images=(load_logo(),),
    )


def _single_line(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def render_budget_alert(
    *,
    budget_name: str,
    period_start: date,
    period_end: date,
    threshold: int,
    spent: Decimal,
    limit: Decimal,
    currency_code: str,
    show_amounts: bool,
    unsubscribe_url: str | None,
) -> RenderedEmail:
    name = _single_line(budget_name, 80)
    percent = (spent / limit * Decimal(100)) if limit > 0 else Decimal(100)
    percent_text = format_percent(percent, under_limit=spent < limit)
    exceeded = spent > limit
    reached_limit = threshold >= 100
    background, color, bar_color = _DANGER if reached_limit else _WARNING
    if reached_limit and exceeded:
        headline = "Superaste el límite de tu presupuesto"
        subject = f"Superaste el límite de tu presupuesto «{name}»"
    elif reached_limit:
        headline = "Llegaste al límite de tu presupuesto"
        subject = f"Alcanzaste el límite de tu presupuesto «{name}»"
    else:
        headline = f"Vas por el {percent_text} de tu presupuesto"
        subject = f"Llevas el {percent_text} de tu presupuesto «{name}»"
    preheader = f"Tu presupuesto «{name}» va en el {percent_text} de su límite."
    return _render(
        "budget_alert",
        subject=subject,
        preheader=preheader,
        footer_reason=(
            "Recibes este correo porque activaste los avisos de presupuesto en Finance AI. "
            "Puedes cambiarlo en Ajustes > Notificaciones."
        ),
        unsubscribe_url=unsubscribe_url,
        headline=headline,
        banner_background=background,
        banner_color=color,
        bar_color=bar_color,
        bar_width=max(2, min(100, int(percent))),
        budget_name=name,
        period_label=format_period(period_start, period_end),
        percent_text=percent_text,
        show_amounts=show_amounts,
        spent_text=format_amount(spent, currency_code),
        limit_text=format_amount(limit, currency_code),
        remaining_label="Excedido" if exceeded else "Disponible",
        remaining_text=format_amount(abs(limit - spent), currency_code),
        remaining_color=_DANGER[1] if exceeded else _OK_COLOR,
        remaining_class="c-danger" if exceeded else "c-ok",
        tone="danger" if reached_limit else "warn",
    )


def render_verification_code(*, code: str, minutes: int) -> RenderedEmail:
    display = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
    return _render(
        "verification_code",
        subject="Tu código de verificación de Finance AI",
        preheader=f"Tu código es {display} y vence en {minutes} minutos.",
        footer_reason=(
            "Recibiste este correo porque alguien pidió verificar esta dirección en Finance AI."
        ),
        code_display=display,
        minutes=minutes,
    )


def render_test_email(*, unsubscribe_url: str | None) -> RenderedEmail:
    return _render(
        "test_email",
        subject="Correo de prueba de Finance AI",
        preheader="Si ves este mensaje, los avisos por correo funcionan en tu cuenta.",
        footer_reason="Pediste este correo de prueba desde Ajustes > Notificaciones en Finance AI.",
        unsubscribe_url=unsubscribe_url,
    )
