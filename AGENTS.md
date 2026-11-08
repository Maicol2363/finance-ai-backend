# Finance AI Backend Agent Guide

This repository owns the FastAPI service, PostgreSQL persistence, migrations, seeds, the versioned OpenAPI contract, and backend tests. Product requirements and architecture decisions live in `NullCorvus/finance-ai`; the Flutter client lives in `Maicol2363/finance-ai-frontend`.

## Branch workflow

- Start story branches from `develop` and target `develop` in feature pull requests.
- Merge `develop` into `main` only for fully tested, release-ready work.
- Preserve completed feature branches unless the team explicitly chooses to remove them.

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

## Staff foundation boundaries

- Migration 0018_staff_foundation adds only the approved four authorization roles and persistence foundations. Keep role assignment, Administrator provisioning, MFA workflows, ticket transitions, field visibility, audit writers, and Auditor queries in authorized backend services, not frontend assumptions.
- Preserve ticket activity and security audit evidence. Ticket messages are customer-visible; escalation and return reasons are restricted staff metadata; resolution summaries are customer-visible. Do not add a general-purpose private-note or message feature.
- Security audit context must use an explicit allowlist. Never record credentials, MFA or invitation secrets, financial amounts, receipts, or sensitive ticket content.
- Keep the append-only security event table and its migration protection. Ordinary application flows must not update or delete audit records.
- The staff portal is the separate React app in Maicol2363/finance-ai-frontend/staff-web/. Configure exact trusted origins with FINANCE_AI_CORS_ALLOWED_ORIGINS; do not use wildcard origins or credentialed CORS. Existing opaque bearer sessions remain authoritative until a browser-session contract is approved.
- Use aidlc-docs/inception/requirements/foundation-schema-mapping.md for the current persistence mapping and outstanding design boundaries. Final Security Auditor scope remains controlled by the authoritative role stories.

For local setup, copy .env.example to .env (Linux: cp .env.example .env; PowerShell: Copy-Item .env.example .env), then use uv sync --all-groups --frozen. docker compose up --build -d starts PostgreSQL and the API; local database access for host-run tools is loopback-only through docker compose -f compose.yaml -f compose.dbeaver.yaml up -d database. The normal API container applies migrations and seeds on startup. Use a database ending in _test for tests.
