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

On Linux, copy the safe example with `cp .env.example .env`; in Windows PowerShell, use `Copy-Item .env.example .env`. Then install the pinned environment with `uv sync --all-groups --frozen`. Start the API and private PostgreSQL database with `docker compose up --build -d`; the API container applies migrations and seeds at startup. To run FastAPI from the host, start the API in Compose or expose PostgreSQL only through the loopback-only DBeaver override. Never commit a real `.env`.

Run PostgreSQL integration and regression tests in the isolated Compose test database with `docker compose --profile test run --rm test`. The test suite refuses database names not ending in `_test`. To change dependencies, use `uv add package@version`, review `pyproject.toml` and `uv.lock`, then verify with `uv sync --all-groups --frozen` and `uv run pip-audit`.

Project documentation and cross-repository orchestration live in `NullCorvus/finance-ai`. The Android/Windows client lives in `Maicol2363/finance-ai-frontend`. This repository owns the generated `openapi.json` contract.

The public identity endpoints are `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `GET /api/v1/auth/me`, and `POST /api/v1/auth/logout`. Access credentials expire after 15 minutes; renewable credentials rotate, are bounded by 30-day idle and 90-day absolute lifetimes, and are never persisted in plaintext. The default maximum is ten active sessions per user.

## Staff foundation

Migration `0018_staff_foundation` adds the approved `support_agent` and `security_auditor` role records, staff invitations, Administrator TOTP persistence, MFA assurance timestamp, ticket/message/activity tables, and append-only security audit events. It preserves the existing `user` and `admin` roles and does not create accounts or alter existing financial data. Support, Administrator, MFA and Auditor workflows are not implemented by this schema foundation; follow the authoritative stories before adding services or routes. Security Auditor query/report decisions remain provisional until the role stories are finalized.

The React staff portal is in the separate `Maicol2363/finance-ai-frontend` repository under `staff-web/`. Its Vite origin is `http://127.0.0.1:5173`; `FINANCE_AI_CORS_ALLOWED_ORIGINS` in this repository's `.env` lists explicit local origins. Browser credentialed CORS is disabled. The current opaque bearer-session endpoints do not define a browser session contract, so sign-in and protected actions remain disabled in the staff portal. The Flutter client and API authentication contract are unchanged.

The schema mapping and limitations are recorded in `aidlc-docs/inception/requirements/foundation-schema-mapping.md`. Apply migrations and run database tests only against an isolated database ending in `_test`; never point this feature branch at production or shared Neon.

The locked TOTP, authenticated-encryption, QR-generation and PDF libraries prepare for later story implementations. No OTP, encryption, QR enrollment or PDF workflow is implemented in this foundation.

## Email notifications (EML-01..EML-03)

Branded HTML emails (with a plain-text alternative and an inline logo) are sent from a transactional outbox, so a notification is decided in the same database transaction as the event that causes it and delivered afterwards by a dispatcher.

- Senders: `console` (development: writes `.eml` and a browsable `.html`, delivers nothing) and `smtp` (a sandbox inbox or any provider that offers SMTP). Configure them with the `FINANCE_AI_EMAIL_*` settings in `.env.example`. Without a real sender, production reports `notifications_unavailable` instead of pretending to deliver.
- Preview every template with sample data: `uv run python scripts/preview_emails.py var/email-preview`, then open the `.html` files in a browser.
- Send a real test email through the configured sender (evidence for the card): `uv run python scripts/send_test_email.py persona@example.com`.
- Send what is waiting in the outbox: `uv run python scripts/dispatch_notifications.py`. The deployed service does the same through `POST /internal/notifications/dispatch` (header `X-Dispatch-Token`), which a scheduler calls every minute.
- Addresses never reach logs or the outbox payload; the dispatcher reads the current address when it sends and re-checks verification and preferences at send time.
- Opt-in (EML-02) is verified: `POST /api/v1/me/email/verification` emails a six-digit code (sent straight from the request, never queued or stored in plain text; 15 minutes, 5 wrong guesses, 60 s between requests, 5 per hour), `POST /api/v1/me/email/verify` checks it and turns email on, `GET/PATCH /api/v1/me/notification-preferences` read and change the master switch, budget alerts and whether amounts appear, and `POST /api/v1/me/notifications/test` sends a test message (3 per hour). Email cannot be turned on for an unverified address (`email_not_verified`).
- Every email carries `List-Unsubscribe` / `List-Unsubscribe-Post` headers and a link to `/public/unsubscribe?token=...` (needs `FINANCE_AI_PUBLIC_BASE_URL`). The token is an HMAC of the account number derived from `FINANCE_AI_SESSION_LIMITER_SECRET`; opening the link only shows a button, and the button or a mail client's one-click POST turns email off. Rotating that secret invalidates old links and open codes; people can still switch notifications off in the app.

## Security Auditor (PB-016, AUD-01–AUD-04)

The `security_auditor` role uses the staff browser session with password plus TOTP (the SEC-01 consumer for this role): the password step returns HTTP 202 and a short-lived challenge; `/api/v1/staff/auth/mfa/enrollment`, `/mfa/verify` and `/mfa/cancel` complete it. Migration `0019_staff_mfa_challenge` adds the challenge table; TOTP secrets are AES-256-GCM encrypted with `FINANCE_AI_STAFF_MFA_ENCRYPTION_KEY` (32 random bytes, base64url). Without that key, Auditor sign-in fails closed and other roles are unaffected.

The Administrator role (ADM-01–ADM-05) lives under `/api/v1/staff/admin/` and uses the same password+TOTP sign-in. Administrators are provisioned only with `scripts/admin_ops.py` (`create-admin`, `promote`, `reset-mfa`); the portal and its API can neither create nor revoke an administrator. Migration `0020_admin_account_management` adds suspension history, MFA recovery codes, session role binding, escalated-ticket ownership, two triggers (`app_user_last_admin_guard`, `app_user_revoke_sessions_on_authority_change`) and two stored procedures (`sp_admin_set_account_suspension`, `sp_admin_return_escalated_ticket`). See `docs/ADMINISTRATOR-MVP.md`.

The read-only Auditor API lives under `/api/v1/staff/audit/` (`catalog`, `events`, `events/{event_id}`, `investigations/{account_id}`, `summary`, `reports`). Every request rechecks the active account, current role, authoritative activation and MFA assurance; Support Agents are denied with a recorded `access.denied`, and `security_auditor` cannot authenticate to the customer API. Query and export bounds are `FINANCE_AI_AUDIT_MAX_QUERY_DAYS` (default 90) and `FINANCE_AI_AUDIT_MAX_REPORT_EVENTS` (default 2000). PDF reports come from one `REPEATABLE READ` snapshot and are delivered only after `audit.report_exported` is durably recorded. See `docs/SECURITY-AUDITOR-MVP.md` for the metric mappings, design decisions and the isolated local demo (`compose.audit-demo.yaml`).

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

## Text capture (HU-013)

Users can describe what happened in their own words and get a proposed operation to review. Nothing is saved while proposing.

- `POST /api/v1/capture/text/proposals` reads one chat turn: a first message, a typed correction (send the `proposal_id`; the server keeps the original text) or a tapped clarification option (`choice`, applied without a new AI call). It returns `ready`, `needs_clarification` (one short question with options) or `unrecognized`.
- `GET` / `DELETE /api/v1/capture/text/proposals/{proposal_id}` read or discard the draft (24 h, private to its owner).
- `POST /api/v1/capture/text/proposals/{proposal_id}/confirm` records the possibly edited values through the same validated, atomic expense, income or transfer service the manual forms use, marked `capture_source = text_ai`. Repeating it returns the first result (`200`, `replayed: true`).

The model only suggests: accounts, categories and income sources are grounded against the caller's own active data, transfers between two owned accounts are never recorded as spending, and a currency or account the user did not name is asked about instead of guessed. The provider is chosen with `FINANCE_AI_LLM_PROVIDER` (`gemini` with `FINANCE_AI_GEMINI_API_KEY`, or `deepseek` with `FINANCE_AI_DEEPSEEK_API_KEY`); the other one is used as a backup when its key is set; keys stay on the server and the client never sees them. A provider outage returns `503 text_capture_unavailable` and the client falls back to the manual forms. Messages are limited per user (burst of 6, then 20 per minute). Drafts live in `text_capture_proposals` (migration `0016`).

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
