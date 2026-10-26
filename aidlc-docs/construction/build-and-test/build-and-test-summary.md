# Build and Test Summary

## Scope and Results

Transfer locking and idempotent replay changes only. Full backend verification completed on 2026-10-19.

- **Test suite**: 419 passed, 0 failed, 0 skipped.
- **Coverage**: 89%.
- **Transfer PostgreSQL integration**: 5 passed, including both concurrency regressions.
- **Static checks**: full-tree Ruff format/check and mypy passed.
- **API contract**: generated OpenAPI comparison clean; no API contract change.
- **Database**: disposable PostgreSQL test database passed `alembic upgrade head`, `alembic check`, and shared seed command.
- **Dependencies**: pip-audit found no known dependency vulnerability; the local root package is not a PyPI distribution and was not audited as a package.
- **Independent review**: Sol found no remaining concrete defects.
- **Warnings**: pytest emitted `ResourceWarning` messages for unclosed SQLite connections in an existing decode-failure test.

## Required Validation

- `uv run ruff format --check src tests`
- `uv run ruff check src tests`
- `uv run mypy src/finance_ai`
- `uv run pytest tests/unit tests/api`
- `uv run pytest tests/integration/test_transfer_persistence.py -q` with safe PostgreSQL test DB
- `uv run pytest --cov=finance_ai --cov-report=term-missing`

All PostgreSQL checks ran against a new loopback-only disposable container without a volume; the container was removed after testing. No existing database or Docker volume was reused.
