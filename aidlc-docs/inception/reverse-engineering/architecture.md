# Finance AI Backend Architecture

## System Overview

The backend is one modular FastAPI service. Feature modules own HTTP routes, Pydantic transport schemas, application services, and persistence adapters. SQLAlchemy persists to PostgreSQL; Alembic migrations are the schema history and `openapi.json` is the client-facing API contract.

## Main Components

- **Application entry point**: `src/finance_ai/main.py:create_app` installs middleware, exception handlers, and the API router.
- **HTTP layer**: `api/router.py` mounts health routes and versioned feature routers. Feature routers parse/serialize DTOs and inject authenticated principals and services.
- **Feature/application layer**: `auth`, `accounts`, `categories`, `income_sources`, `expenses`, `incomes`, `budgets`, `transfers`, `receipts`, `capture`, and `dashboard`. Routers are the HTTP controllers; feature `service.py` modules enforce rules and coordinate persistence.
- **Persistence**: `db/session.py`, SQLAlchemy ORM models under `db/models/`, feature repositories, and UoW implementations under `db/`. Alembic migration scripts define executable schema changes.
- **Transport and persistence schemas**: Feature `schemas.py` contains Pydantic request/response DTOs; `db/models/` contains ORM entities. `openapi.json` captures the public HTTP surface.
- **Authentication and dependency injection**: Feature `dependencies.py` creates services; `auth/dependencies.py` validates bearer access and yields a principal. Services/repositories receive the principal's `user_id` as `owner_id` for tenant-scoped reads and writes.
- **Configuration**: `core/config.py` provides Pydantic Settings from environment variables; provider credentials use secret-valued settings.
- **External AI**: `capture/gateway.py` and `receipts/gateway.py` isolate configurable text and vision providers. Capture proposals are confirmed by calling ordinary financial application services.
- **Errors and observability**: `core/errors.py` translates application and unexpected exceptions into safe problem responses; middleware and `core/observability.py` correlate requests.
- **Deployment**: Docker/Compose, Cloud Run scripts, and VM/Nginx artifacts are in the repository root, `deploy/`, and `infrastructure/`.
- **Tests**: `tests/api`, `tests/unit`, `tests/integration`, and `tests/property`, with shared fixtures in `tests/conftest.py`.

## Transfer Flow: HTTP to PostgreSQL

1. `POST /api/v1/transfers` enters `transfers/router.py:create_transfer` with `TransferCreateRequest`.
2. FastAPI resolves the authenticated principal and `TransferService` through dependencies.
3. `TransferService.create` delegates to `create_with_replay` inside a SQLAlchemy transaction.
4. The service checks proposal replay, validates distinct active owner-owned accounts and currency/amount rules, then asks `TransferRepository` to persist.
5. The repository creates one confirmed `FinancialTransaction` and two `AccountMovement` records: negative source and positive destination. ORM constraints and composite foreign keys preserve owner scope.
6. The service reconstructs a `TransferRecord`; transaction context commits on success and rolls back on exceptions. The router returns `TransferResponse` with the existing 201 contract.

## Current Architectural Notes

- Most modules use an explicit Unit of Work; income, transfer, and capture modules also contain direct session-factory transaction scopes. This variation alone was not treated as a defect.
- Financial operation rules are service-owned; repositories perform database reads/writes.
- Transfer account rows are locked with `FOR UPDATE`; lock ordering and concurrent replay handling are corrected in this branch.
- No frontend changes, API contract changes, ORM schema changes, or migrations are included.

## Integration Points

- PostgreSQL through synchronous SQLAlchemy/psycopg.
- Configured Gemini/DeepSeek-compatible AI providers through gateways.
- Flutter consumes the versioned FastAPI endpoints and checked-in `openapi.json`.

## Applicable Security Compliance

- **SECURITY-08, application access control**: compliant for the changed replay path. Both the initial and post-conflict lookup filter by `owner_id`; authenticated principal injection remains in the existing router/dependency path.
- **SECURITY-15, exception handling and fail-safe defaults**: compliant for the changed transaction path. Session/transaction context managers roll back and close resources before replay recovery; if no committed owner-scoped winner exists, the integrity error is re-raised to the existing safe global handler.
- **SECURITY-05, input validation**: existing request DTO validation is unchanged; no validation surface was modified.
- **Other baseline controls**: N/A to this localized service/test change; no infrastructure, network, logging, credential, deployment, or dependency configuration was altered.
- **Property-Based Testing**: N/A for this unit; the change is transaction/locking coordination rather than a pure function or serialization transformation.

## Supplemental Review with `fba`

- **Installed skill**: `fba` from `fastapi-practices/skills`, at `/home/d3ad/.codex/skills/fastapi-best-architecture`. Its `SKILL.md`, `references/coding-style.md`, and `references/naming.md` were read.
- **Applicability**: The skill describes an API/Schema/Service/CRUD/Model layout for its own FastAPI Best Architecture framework. Finance AI uses routers, Pydantic schemas, application services, repositories, and ORM models, so the layer-boundary and responsibility checks transfer; its CRUDPlus naming/plugin conventions do not.
- **Lock ordering**: `TransferService._validate_references` now resolves the two account IDs in ascending order and still maps them back to source/destination roles. Both request directions use the same acquisition order, removing the two-row lock cycle without moving persistence responsibility.
- **Idempotency boundary**: `create_with_replay` owns the transaction and its error recovery. The context manager rolls back/closes the failed transaction; a fresh session looks up the owner-scoped winning proposal. It returns the committed transfer only when that winner exists, and re-raises otherwise. No API/schema/repository layer was added.
- **Test boundary**: the deterministic lock-order test exercises the existing reference-validation operation and replaces only its repository boundary; no production API or helper was added for tests. PostgreSQL tests cover concurrent confirmation/persistence.
- **Outcome**: no additional architecture defect found in the current diff. The skill's advice to make all I/O async was not applied: this repository uses synchronous SQLAlchemy and synchronous FastAPI path operations; changing that would be a wider architectural redesign unrelated to the scoped defects.
