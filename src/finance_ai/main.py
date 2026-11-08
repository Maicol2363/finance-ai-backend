from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from finance_ai.api.router import api_router
from finance_ai.core.config import get_settings
from finance_ai.core.errors import install_exception_handlers
from finance_ai.core.logging import configure_logging
from finance_ai.core.middleware import RequestGuardMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Finance AI API",
        version="0.1.0",
        description="AI-assisted personal finance and budgeting API",
    )
    install_exception_handlers(app)
    app.add_middleware(RequestGuardMiddleware, max_body_bytes=settings.max_request_body_bytes)
    # Starlette places the most recently added user middleware on the outside.
    # Keep CORS outside the request guard so its early error responses get CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs", status_code=307)

    return app


app = create_app()
