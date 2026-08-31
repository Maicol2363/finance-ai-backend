from fastapi import FastAPI

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
        description="Manual-first personal budgeting API",
    )
    install_exception_handlers(app)
    app.add_middleware(RequestGuardMiddleware, max_body_bytes=settings.max_request_body_bytes)
    app.include_router(api_router)
    return app


app = create_app()
