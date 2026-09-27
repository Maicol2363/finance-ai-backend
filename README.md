# Finance AI Backend

FastAPI backend for the manual-first Finance AI application. The current scope provides registration, opaque hash-only sessions, authenticated financial-account management with exact derived balances, expense correction, and unified financial history.

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

## Expenses (HU-008)

HU-008 exposes the expense records created by the transaction/receipt flow:

- `GET /api/v1/expenses` lists only the authenticated user's expenses with stable pagination.
- `GET /api/v1/expenses/{expense_id}` returns one owned expense.
- `PATCH /api/v1/expenses/{expense_id}` corrects editable expense, receipt, and transaction fields.
- `DELETE /api/v1/expenses/{expense_id}` removes the complete confirmed expense transaction.

Only `confirmed` expenses can be edited or deleted. An update recalculates the receipt total and the signed account movement inside the same database transaction. Deleting the financial transaction uses PostgreSQL cascades for its receipt, expense items, and account movements. Detail responses expose `receipt_image` and `image_path` as persisted attachment references; the current backend removes those references, while physical object-storage cleanup belongs to the future file-storage adapter. Expense monetary values follow the database `NUMERIC(12,2)` limit.

The service never trusts an identifier by itself: every read and mutation scopes the expense, receipt, transaction, movement, account, and category checks to the authenticated owner. `0009_hu008_hu011_expense_history` adds the financial linkage and editable fields; `0010_expense_audit_index` adds audit timestamps and repairs the category/date index for databases that already reached `0009`.

## Budgets (HG-016)

Authenticated users can create multiple concurrent budgets for one owned category or all expenses. Each budget has an optional custom name, exact currency, amount, and either a recurring schedule or a one-time date range. Recurring schedules support every 1–999 days, weeks, months, or years; weekly and monthly remain backward-compatible simple presets.

Budget progress is derived on read from confirmed expense transactions. Only expenses inside the resolved occurrence and in the budget's exact currency contribute; no implicit currency conversion or separately maintained progress total is used. Category ownership is enforced at both the service and database constraint layers, and deleting a category cascades only its linked budgets.

## Unified history (HU-011)

`GET /api/v1/history` returns owned `expense`, `income`, and `transfer` transactions ordered by `occurred_at DESC, id DESC`. It supports `search`, `type`, `category_id`, `account_id`, `period=weekly|monthly|custom`, `start_date`, `end_date`, `page`, and `page_size`. Custom ranges are inclusive by date, and each movement includes the account's currency metadata and signed amount. History reads existing income and transfer transactions; their public creation flows belong to later stories.

### Backend verification

Run the following from this directory after PostgreSQL is healthy:

```text
docker compose build test
docker compose --profile test run --rm test
docker compose exec -T api alembic check
```

The integration suite downgrades the configured database to `base`, upgrades it to `head`, seeds shared catalogs, and refuses any database whose name does not end in `_test`.

Downgrading below `0009` is intentionally destructive for expenses without a subcategory: revision `0008` cannot represent uncategorized items, so `0009` removes those incompatible rows before restoring its old constraint.
