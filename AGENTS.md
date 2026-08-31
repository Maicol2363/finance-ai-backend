# Finance AI Backend Agent Guide

This repository owns the FastAPI service, PostgreSQL persistence, migrations, seeds, the versioned OpenAPI contract, and backend tests. Product requirements and architecture decisions live in `NullCorvus/finance-ai`; the Flutter client lives in `Maicol2363/finance-ai-frontend`.

## Mandatory error and privacy contract

- Every public failure uses a stable machine-readable code, a safe plain-language detail, and the request/correlation ID.
- Structured logs preserve the same correlation ID so copied client reports can be matched to server events.
- Never expose or log passwords, password confirmations, email addresses, access/renewable credentials, credential hashes, authorization headers, request bodies, SQL, environment values, stack traces, raw exceptions, or URL credentials/query values.
- Translate internal exceptions at the API boundary and test the public code, message, headers, correlation behavior, and absence of secrets.
- Keep responses compatible with the shared client diagnostic contract; do not add feature-specific raw error shapes.

## Architecture and data rules

- Keep one modular FastAPI service for the MVP; do not create microservices without an approved architecture decision.
- Application services own transactions. Repositories stage persistence and never commit independently.
- Alembic migrations are the executable schema history; SQLAlchemy models are the persistence definition.
- `openapi.json` is the frontend-facing API authority and must be regenerated and reviewed after intentional contract changes.
- Ordinary budgeting calculations and validation are deterministic. Future AI is optional input assistance and calls the same application services as manual forms.
- PostgreSQL remains on a private Compose network. The only local host access is the loopback-only, read-only DBeaver override and its hash-free views.

## Required verification

Run formatting, linting, type checking, migration/seed drift checks, OpenAPI drift checks, tests with at least 80% line coverage, real PostgreSQL integration tests when persistence changes, and the dependency audit. Never use a non-test database for automated tests.
