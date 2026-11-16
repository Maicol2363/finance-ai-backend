"""Shared PostgreSQL integration hygiene."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module", autouse=True)
def discard_module_audit_evidence() -> Iterator[None]:
    """Empty the test audit store after each module.

    Customer sign-in now records SEC-02 evidence, and migration 0018 refuses to drop a table that
    holds audit evidence, so a later module that migrates down would fail on rows left by an
    earlier one. Only the isolated ``*_test`` database is ever touched.
    """
    yield
    url = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
    if not url or not (make_url(url).database or "").endswith("_test"):
        return
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            if inspect(connection).has_table("security_audit_event"):
                connection.execute(text("TRUNCATE TABLE security_audit_event"))
    finally:
        engine.dispose()
