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
- AUD-01–AUD-04 are implemented in `src/finance_ai/audit/` (read-only queries, aggregates and PDF export) with the Security Auditor password+TOTP sign-in in `src/finance_ai/staff_auth/`. Keep the role sets in `core/roles.py` authoritative, keep every Auditor operation behind `get_security_auditor_user`, and never add audit update/delete routes. 
- ADM-01–ADM-05 and the shared SEC-01 recovery/replacement are implemented in `src/finance_ai/admin/` and `src/finance_ai/staff_auth/recovery.py` (see `docs/ADMINISTRATOR-MVP.md`). Every admin route sits behind `get_admin_user` (current role, active account, active authenticator, MFA-verified session). Never add a portal route that creates, grants or revokes `admin`; provisioning stays in `scripts/admin_ops.py`. The last-active-administrator invariant, session revocation on suspension or role change, atomic suspension and the escalated-ticket return are enforced by the PostgreSQL routines in migration `0020_admin_account_management` and must not be bypassed or duplicated in application code. Critical changes and their SEC-02 event share one transaction. Row locks (`FOR UPDATE`) on entities with joined relationships need `lazyload('*')` and `of=`; SQLite unit tests cannot detect this, so cover them with PostgreSQL integration tests.

For local setup, copy .env.example to .env (Linux: cp .env.example .env; PowerShell: Copy-Item .env.example .env), then use uv sync --all-groups --frozen. docker compose up --build -d starts PostgreSQL and the API; local database access for host-run tools is loopback-only through docker compose -f compose.yaml -f compose.dbeaver.yaml up -d database. The normal API container applies migrations and seeds on startup. Use a database ending in _test for tests.
