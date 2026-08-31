from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from finance_ai.auth.dependencies import get_session_service, get_unsafe_password_provider
from finance_ai.db.models.reference import Currency
from finance_ai.db.session import get_session_factory

router = APIRouter(tags=["Health"])


@router.get("/health/live", status_code=status.HTTP_200_OK)
def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=None)
def readiness() -> dict[str, str] | JSONResponse:
    try:
        get_unsafe_password_provider().ensure_ready()
        get_session_service().ensure_ready()
        with get_session_factory()() as session:
            session.execute(text("SELECT 1"))
            cop = session.scalar(
                select(Currency.id).where(Currency.code == "COP", Currency.is_active.is_(True))
            )
            if cop is None:
                raise RuntimeError("Required reference data is unavailable")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}
