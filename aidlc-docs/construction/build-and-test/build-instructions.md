# Build Instructions

## Prerequisites

- Python 3.14 and uv.
- Install locked project dependencies with `uv sync --all-groups`.
- PostgreSQL is needed only for tests marked integration; configure `FINANCE_AI_TEST_DATABASE_URL` to a disposable database whose name ends in `_test`.

## Build and Static Checks

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src/finance_ai
```

Do not point automated tests at a non-test database.
