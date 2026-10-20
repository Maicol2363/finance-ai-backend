# Execution Plan: Transfer Concurrency Fixes

## Scope and Rationale

One backend unit addresses two confirmed races in `transfers/service.py`. Keep the current modular service/repository boundary. No broad redesign is warranted.

## Sequence

1. [x] Reconstruct backend modules and document the transfer path.
2. [x] Confirm lock-order and replay races in the transfer service/repository/schema and identify integration-test gaps.
3. [x] Change account row-lock acquisition to ascending account ID.
4. [x] Recover a concurrent unique proposal winner as an idempotent replay after transaction rollback.
5. [x] Add PostgreSQL concurrency regressions for opposite transfers and repeated proposal confirmation.
6. [x] Run focused integration tests, format/lint, type checks, and full suite/coverage against a disposable test database.
7. [x] Obtain independent read-only review and record remaining risks.

## Results

- Full backend suite: 419 passed, 0 failed, 0 skipped; 89% coverage.
- Ruff format/check and mypy: passed across the full source/test tree.
- OpenAPI drift: clean. `pip-audit`: no known dependency vulnerabilities; the local root package is not published to PyPI and was not audited as an installed distribution.
- Disposable PostgreSQL checks: transfer integration, migration upgrade, Alembic drift check, and shared seed command passed.
- Independent Sol review: no remaining concrete findings.
- Warning: existing SQLite test emitted `ResourceWarning` messages about unclosed connections.

## Workflow

Architecture discovery → requirements and scope → one transfer unit → focused regressions → static checks → full verification → independent review.

Text alternative: all work stays in the backend worktree; no frontend or external deployment work is in scope.
