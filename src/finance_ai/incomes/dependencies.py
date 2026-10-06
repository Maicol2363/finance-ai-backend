from finance_ai.db.session import get_session_factory
from finance_ai.incomes.service import IncomeService


def get_income_service() -> IncomeService:
    return IncomeService(get_session_factory())
