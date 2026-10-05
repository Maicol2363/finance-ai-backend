from fastapi import APIRouter

from finance_ai.accounts.router import router as accounts_router
from finance_ai.api.health import router as health_router
from finance_ai.auth.router import router as auth_router
from finance_ai.budgets.router import router as budgets_router
from finance_ai.capture.router import router as capture_router
from finance_ai.categories.router import router as categories_router
from finance_ai.expenses.router import router as expenses_router
from finance_ai.income_sources.router import router as income_source_router
from finance_ai.incomes.router import router as incomes_router
from finance_ai.receipts.router import router as receipts_router
from finance_ai.transfers.router import router as transfers_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router, prefix="/api/v1")
api_router.include_router(accounts_router, prefix="/api/v1")
api_router.include_router(categories_router, prefix="/api/v1")
api_router.include_router(income_source_router, prefix="/api/v1")
api_router.include_router(expenses_router, prefix="/api/v1")
api_router.include_router(incomes_router, prefix="/api/v1")
api_router.include_router(budgets_router, prefix="/api/v1")
api_router.include_router(receipts_router, prefix="/api/v1")
api_router.include_router(transfers_router, prefix="/api/v1")
api_router.include_router(capture_router, prefix="/api/v1")
