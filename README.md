# Finance AI Backend

FastAPI backend for the manual-first Finance AI application. The current scope provides registration, opaque hash-only sessions, and authenticated financial-account management with exact derived balances.

## Local commands

From this directory:

- Install/sync: `uv sync --all-groups --frozen`
- Run API: `uv run uvicorn finance_ai.main:app --reload`
- Migrate: `uv run alembic upgrade head`
- Seed: `uv run python -m finance_ai.db.seeds`
- Test: `uv run pytest --cov=finance_ai --cov-report=term-missing`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Types: `uv run mypy src`

Start the standalone API and private PostgreSQL database with `docker compose up --build -d`. Run `docker compose --profile test run --rm test pytest -m integration` for real PostgreSQL integration tests. Never commit a real `.env`.

Project documentation and cross-repository orchestration live in `NullCorvus/finance-ai`. The Android/Windows client lives in `Maicol2363/finance-ai-frontend`. This repository owns the generated `openapi.json` contract.

The public identity endpoints are `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `GET /api/v1/auth/me`, and `POST /api/v1/auth/logout`. Access credentials expire after 15 minutes; renewable credentials rotate, are bounded by 30-day idle and 90-day absolute lifetimes, and are never persisted in plaintext. The default maximum is ten active sessions per user.

## Financial accounts (HU-003)

Authenticated users can manage the places where their money is held:

- `POST /api/v1/accounts` creates an owned account.
- `GET /api/v1/accounts?include_inactive=false` lists owned accounts and form options.
- `GET /api/v1/accounts/{account_id}` returns owned active or inactive detail.
- `PATCH /api/v1/accounts/{account_id}` edits account metadata.
- `PATCH /api/v1/accounts/{account_id}/status` deactivates or reactivates an account.
- `DELETE /api/v1/accounts/{account_id}` permanently deletes an unused owned account.

Permanent deletion succeeds only when the account has no movements of any status; opening balance alone does not block it. A used account returns the stable `account_delete_blocked` conflict and must be deactivated so its history remains intact. History migration and advanced delete-all behavior are deferred until the public transaction and receipt stories define their complete dependencies. Supported account types are `cash`, `bank`, `digital_wallet`, `savings`, `credit_card`, and `other`.

Every account stores an explicit supported currency and signed `NUMERIC(18,2)` opening balance. API money is serialized as a fixed decimal string. Current balance is never stored as mutable state; it is calculated as opening balance plus confirmed signed account movements. Pending and voided transactions do not contribute. HU-003 creates that internal movement persistence only—there is no public transaction or movement write endpoint yet.

Account names are unique case-insensitively among one user's active accounts. Inactive names may be reused, but reactivation fails if the name now conflicts. Currency may change until the account has confirmed activity. All reads and mutations derive ownership from the bearer session, and missing and foreign-owned identifiers share the same safe `account_not_found` response.
