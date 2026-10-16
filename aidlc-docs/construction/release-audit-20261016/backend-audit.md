# Finance AI backend audit — 2026-10-16

## Scope and result

Reviewed the FastAPI routes, role model, authenticated financial operations, AI proposal confirmation, public error handling, OpenAPI contract, and automated backend suite. The review ran on `codex/release-audit-20261016`, based on `6c7c9fc`. No existing database was used; PostgreSQL persistence checks ran against a new disposable PostgreSQL 18.6 container with no mounted volumes.

Two release-relevant defects were corrected:

- Text AI confirmation now persists its proposal UUID on the resulting expense, income, or transfer. A unique `(user_id, source_proposal_id)` constraint prevents duplicate financial operations if the financial commit succeeds and the subsequent proposal update fails. A retry retrieves the prior operation and can finish storing the confirmation.
- Registration now resolves the `user` role by name instead of relying on role ID `1`. This prevents an existing database with a different role insertion order from assigning a new account the `admin` role.
- Unexpected API failures no longer call `logger.exception`; logs retain the correlation ID and exception type without attaching raw exception text or a traceback. The client continues to receive the stable `internal_error` response.
- A post-commit retry that changes confirmation values now builds the expense confirmation from the saved expense amount and timestamp. Income and transfer already returned saved values; new fault-injection cases verify all three intent paths return the original committed amount and date, with no duplicate movement or balance change.

## Route and role inventory

The checked-in OpenAPI contract describes 32 paths:

| Area | Paths | Access |
|---|---|---|
| Health | `GET /health/live`, `GET /health/ready` | Public |
| Authentication | `POST /api/v1/auth/register`, `/login`, `/refresh`, `/logout`; `GET /api/v1/auth/me` | Register/login/refresh do not require a bearer session; logout uses a renewable credential in its body; `/me` requires bearer access |
| Accounts | `/api/v1/accounts`, `/api/v1/accounts/{account_id}`, `/status` | Authenticated, owner-scoped |
| Budgets | `/api/v1/budgets`, `/api/v1/budgets/{budget_id}` | Authenticated, owner-scoped |
| Categories | `/api/v1/categories`, `/order`, `/{category_id}` | Authenticated, owner-scoped |
| Expenses and history | `/api/v1/expenses`, `/{expense_id}`, `/api/v1/history` | Authenticated, owner-scoped |
| Income and sources | `/api/v1/incomes`, `/{income_id}`, `/api/v1/income-sources`, `/{income_source_id}` | Authenticated, owner-scoped |
| Transfers | `POST /api/v1/transfers` | Authenticated, owner-scoped |
| Dashboard | `GET /api/v1/dashboard/summary` | Authenticated, owner-scoped |
| Receipt capture | `/api/v1/receipts/proposal`, `/{proposal_id}`, `/{proposal_id}/retry`, `/{proposal_id}/confirm`, `/{proposal_id}` (DELETE), `/scan` | Authenticated; proposal confirmation is explicit; legacy `/scan` returns extraction data only |
| Text AI capture | `/api/v1/capture/text/proposals`, `/{proposal_id}`, `/{proposal_id}/confirm`, `/{proposal_id}` (DELETE) | Authenticated; proposal creation does not persist a financial record; confirmation is explicit |

The database seeds `user` and `admin`, but the public authenticated principal contains only user ID, email, and preferred currency. There are no administrator-only routes, premium entitlement/subscription model, or server-side premium checks. Receipt and text AI routes therefore accept any authenticated account. The repository has no current product contract or data model from which to safely decide premium entitlement. Treat premium/admin enforcement as not implemented and a release blocker if the presentation promises those tiers; do not claim that hiding a client control enforces access.

## Financial and AI integrity findings

- Manual expenses, incomes, and transfers use the authenticated owner and validate their account/category references. Existing integration coverage exercises persistence and cross-owner restrictions.
- Dashboard and financial transaction queries exclude voided transactions where relevant; the schema restricts transaction types and statuses.
- Receipt confirmation already uses one expense unit of work to create the financial transaction and mark the proposal confirmed. It rejects non-confirmable/discarded/expired proposals and supports replay by transaction ID.
- Text AI confirmation previously opened a proposal transaction, then called independently committing financial services. A crash between their commits could leave the proposal open after the financial record was saved, allowing a retry to create a duplicate. The new internal proposal key plus database uniqueness makes these retries idempotent across expense, income, and transfer operations. Replays return the persisted operation’s values even when the retried command has a different amount/date. Fault injection after the financial commit proves retry leaves one transaction and one balance change for each intent.
- Confirmation values still pass through the same deterministic application services as manual entry; the model does not directly write financial records.

## Verification

| Check | Result |
|---|---|
| Full unit, API, property, and PostgreSQL integration suite | 413 passed; 0 skipped; 47 PostgreSQL integration tests ran |
| Total branch coverage | 89% |
| Ruff lint | Passed |
| Ruff format check | Passed; 221 files formatted |
| mypy | Passed; 140 source files |
| OpenAPI drift (`scripts/export_openapi.py --check`) | Passed; no public schema change from the internal idempotency key |
| `pip-audit` | No known vulnerabilities; local project package was skipped because it is not published on PyPI |
| Isolated test API | `/health/ready` returned 200 on `127.0.0.1:8016` |

The full suite reports non-failing SQLite `ResourceWarning`s from existing test code. The isolated service on port 8016 was restarted from the updated source and `/health/live` plus `/health/ready` both returned 200. The full suite used only `FINANCE_AI_TEST_DATABASE_URL`, pointed to the disposable database whose name ends in `_test`; the real backend database was not changed.

Commands run:

```bash
FINANCE_AI_TEST_DATABASE_URL=<private-isolated-test-database-url> uv run pytest --cov=finance_ai --cov-report=term:skip-covered -q
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run python scripts/export_openapi.py --check
uv run pip-audit
```

## Changed files

- `src/finance_ai/capture/service.py`
- `src/finance_ai/core/errors.py`
- `src/finance_ai/db/models/transactions.py`
- `src/finance_ai/db/repositories/expenses.py`
- `src/finance_ai/db/repositories/registration.py`
- `src/finance_ai/expenses/repositories.py`
- `src/finance_ai/expenses/service.py`
- `src/finance_ai/incomes/repository.py`
- `src/finance_ai/incomes/service.py`
- `src/finance_ai/transfers/repository.py`
- `src/finance_ai/transfers/service.py`
- `migrations/versions/0017_text_capture_financial_idempotency.py`
- `tests/capture_support.py`
- `tests/integration/test_income_persistence.py`
- `tests/integration/test_registration_persistence.py`
- `tests/integration/test_text_capture_persistence.py`
- `tests/test_error_handling.py`

No commit, push, or merge was performed. For physical UI validation, the isolated test API is running at `http://127.0.0.1:8016`; its private environment file is `/tmp/finance-audit-test-env.json` with mode `0600`. The integration fixtures drop/recreate the test schema, so do not run the integration suite while the physical UI session is using that API.
