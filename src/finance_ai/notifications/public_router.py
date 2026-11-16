"""The page behind the unsubscribe link in every email.

Opening the link only shows a confirmation button (mail scanners and link previews open links,
and must not change anything). The button, or a mail client's one-click request, POSTs to the
same address. The token is signed and carries only the account number, so there is nothing to
look up before it is checked and no way to learn whether an address exists.
"""

from __future__ import annotations

from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from finance_ai.core.config import Settings, get_settings
from finance_ai.notifications.account import NotificationAccountService
from finance_ai.notifications.dependencies import get_notification_account_service
from finance_ai.notifications.tokens import read_unsubscribe_token

router = APIRouter(prefix="/public", include_in_schema=False)

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}
_PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Finance AI</title>
<style>
body{{margin:0;background:#f3f6f4;color:#1c2b25;font:16px/1.5 system-ui,sans-serif}}
main{{max-width:30rem;margin:12vh auto;padding:2rem;background:#fff;border-radius:12px;
box-shadow:0 1px 4px rgba(0,0,0,.12)}}
h1{{font-size:1.25rem;margin:0 0 .75rem}}
button{{font:inherit;font-weight:600;padding:.7rem 1.2rem;border:0;border-radius:8px;
background:#1e6b4f;color:#fff;cursor:pointer}}
@media (prefers-color-scheme:dark){{body{{background:#101814;color:#e4ece8}}
main{{background:#18231d}}}}
</style></head>
<body><main><h1>{title}</h1>{body}</main></body></html>"""

_ASK_TITLE = "¿Dejar de recibir avisos por correo?"
_DONE_TITLE = "Listo, no recibirás más avisos por correo"
_BAD_TITLE = "Este enlace no es válido"
_IN_APP = "Puedes cambiarlo cuando quieras en la app: Ajustes &gt; Notificaciones."


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=title, body=body), status_code=status, headers=_HEADERS)


@router.get("/unsubscribe", response_class=HTMLResponse)
def confirm_unsubscribe(
    token: Annotated[str, Query(max_length=120)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    if read_unsubscribe_token(settings, token) is None:
        return _page(_BAD_TITLE, f"<p>El enlace está incompleto o ya no sirve. {_IN_APP}</p>", 400)
    form = (
        f"<p>Dejarás de recibir los avisos de Finance AI en este correo. {_IN_APP}</p>"
        f'<form method="post" action="/public/unsubscribe?token={escape(token, quote=True)}">'
        '<button type="submit">Sí, dejar de recibirlos</button></form>'
    )
    return _page(_ASK_TITLE, form)


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(
    token: Annotated[str, Query(max_length=120)],
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[NotificationAccountService, Depends(get_notification_account_service)],
) -> HTMLResponse:
    user_id = read_unsubscribe_token(settings, token)
    if user_id is None:
        return _page(_BAD_TITLE, f"<p>El enlace está incompleto o ya no sirve. {_IN_APP}</p>", 400)
    service.unsubscribe(user_id)  # same answer whether or not the account still exists
    return _page(_DONE_TITLE, f"<p>{_IN_APP}</p>")
