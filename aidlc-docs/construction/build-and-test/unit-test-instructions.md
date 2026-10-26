# Unit Test Instructions

Run unit and API/contract tests without PostgreSQL:

```bash
uv run pytest tests/unit tests/api
```

Run all test groups and line coverage:

```bash
uv run pytest --cov=finance_ai --cov-report=term-missing
```

Integration tests skip unless `FINANCE_AI_TEST_DATABASE_URL` points to a disposable database ending in `_test`.
