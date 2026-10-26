# Integration Test Instructions

## Transfer Concurrency

Use an isolated PostgreSQL database named with `_test` suffix and set `FINANCE_AI_TEST_DATABASE_URL`. The module migrates the configured test DB and truncates application users between tests.

```bash
uv run pytest tests/integration/test_transfer_persistence.py -q
```

The scenarios cover normal same/cross-currency transfer, validation rollback, opposite-direction concurrent transfers, and simultaneous confirmation of one proposal. Never use a development, staging, or production database.
