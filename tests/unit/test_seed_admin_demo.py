"""The Administrator demo seed refuses anything except the isolated demo database."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from finance_ai.core.config import get_settings


def load_seed():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_admin_demo.py"
    spec = importlib.util.spec_from_file_location("seed_admin_demo_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("environment", "database"),
    [
        ("local", "finance_ai"),
        ("local", "finance_ai_test"),
        ("production", "admin_demo_test"),
    ],
)
def test_seed_refuses_other_databases_and_environments(
    monkeypatch: pytest.MonkeyPatch, environment: str, database: str
) -> None:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", environment)
    monkeypatch.setenv(
        "FINANCE_AI_DATABASE_URL",
        f"postgresql+psycopg://u:p-secret-value-1@127.0.0.1:5432/{database}",
    )
    if environment == "production":
        monkeypatch.setenv("FINANCE_AI_SESSION_LIMITER_SECRET", "a-production-limiter-secret-value")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="isolated admin_demo_test"):
            load_seed().seed()
    finally:
        get_settings.cache_clear()
