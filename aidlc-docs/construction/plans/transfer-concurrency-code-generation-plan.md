# Code Generation Plan: Transfer Concurrency and Replay

## Unit Context

- **Unit**: `transfer-concurrency-and-replay`
- **Stories**: prevent deadlock between opposite-direction transfers; preserve idempotent replay under concurrent confirmation.
- **Dependencies**: existing PostgreSQL schema, transfer service/repository, pytest integration fixture.
- **Contracts**: no public DTO, endpoint, ORM, or migration changes.

## Steps

1. [x] Inspect transfer service, repository, unique proposal constraint, and existing integration tests.
2. [x] Acquire owner-scoped account locks in ascending account ID while retaining source/destination meaning.
3. [x] On an integrity conflict for a proposal confirmation, allow rollback, query the committed owner-scoped transfer in a fresh session, and return replay if it exists; otherwise re-raise.
4. [x] Add synchronized PostgreSQL regression for a concurrent duplicate proposal.
5. [x] Add concurrent opposite-direction transfer regression with final ledger/balance assertions.
6. [x] Run focused PostgreSQL integration and full quality checks using a disposable test database.
7. [x] Run full suite and obtain independent review.

## Results

- PostgreSQL transfer integration: 5 passed.
- Full suite: 419 passed, 0 failed, 0 skipped; 89% coverage.
- Ruff, mypy, OpenAPI drift, migrations, Alembic drift check, and seed command passed.
- Sol review: no remaining concrete findings.
