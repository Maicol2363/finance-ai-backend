from fastapi import APIRouter

from finance_ai.api.health import router as health_router
from finance_ai.auth.router import router as auth_router
from finance_ai.income_sources.router import router as income_source_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router, prefix="/api/v1")
api_router.include_router(income_source_router, prefix="/api/v1")
