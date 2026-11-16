# Email notifications: how it works and how to run it

Status (2026-11-16): built and tested against the console sender, a real-socket SMTP sink and PostgreSQL. **No provider account, domain or DNS exists yet**; those are the open decisions listed at the end. Nothing here has sent a message to a real mailbox.

## What a person gets

| Email | When | Needs |
|---|---|---|
| Verification code | The person asks to verify their address (Ajustes > Notificaciones) | Nothing: sent straight from the request |
| Test email | The person taps "Enviarme un correo de prueba" | Verified address |
| Budget alert | Spending reaches 80 % or 100 % of an active budget's limit in its current period | Verified address, email on, budget alerts on |

Every email is HTML (table layout, inline styles, inline logo, dark-mode rules, preheader) with a plain-text alternative. Budget alerts and test emails carry `List-Unsubscribe` and `List-Unsubscribe-Post` headers and a visible unsubscribe link when `FINANCE_AI_PUBLIC_BASE_URL` is set.

## How it flows

1. **Decide.** `BudgetAlertEvaluator` runs inside the expense transaction (create, receipt confirmation, edit) and writes at most one `notification_outbox` row per budget, period, threshold and limit (`dedupe_key`, unique). It never blocks the expense: it runs in a savepoint and a failure is logged by type only.
2. **Deliver.** `NotificationDispatcher` claims due rows with `FOR UPDATE SKIP LOCKED`, sends outside any transaction, and records the result. Cloud Run only guarantees CPU while a request is served, so a scheduler calls `POST /internal/notifications/dispatch` every minute.
3. **Re-check at send time.** Account still exists and active, address verified, email on, budget alerts on, daily cap not reached. Otherwise the row is `skipped` with a reason.
4. **Retry.** Retryable provider failures back off 1, 5, 15, 60 and 240 minutes (`FINANCE_AI_EMAIL_MAX_ATTEMPTS`, default 5), then `failed`. A row whose instance died mid-send returns to `pending` after 10 minutes (at-least-once delivery).

Alert rules worth knowing: an expense that jumps past both thresholds sends only the 100 % email; spending that falls back and rises again does not repeat an alert in the same period; changing the limit or starting a new period re-arms it. Editing a budget does not send mail by itself, and deleting an expense never does.

## Settings

All are `FINANCE_AI_*` environment variables (see `.env.example`).

| Setting | Default | Notes |
|---|---|---|
| `EMAIL_PROVIDER` | `console` | `console` writes `.eml` and `.html` files and delivers nothing; `smtp` sends. In production the console sender is refused: features report `notifications_unavailable` and the app hides them |
| `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `EMAIL_REPLY_TO` | unset, `Finance AI`, unset | The from address must belong to a domain you authenticated with the provider |
| `EMAIL_SMTP_HOST/PORT/USERNAME/PASSWORD` | unset, 587 | Username and password must both be set or both be empty |
| `EMAIL_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` (`none` is refused in production) |
| `PUBLIC_BASE_URL` | unset | Public https origin of the API, no trailing slash; builds unsubscribe links |
| `NOTIFICATION_DISPATCH_TOKEN` | unset | At least 32 characters. Unset keeps the dispatch endpoint disabled (503) |
| `EMAIL_DAILY_CAP_PER_USER` | 10 | Counts emails actually sent in the last 24 hours |
| `EMAIL_VERIFICATION_MINUTES` / `_MAX_ATTEMPTS` | 15 / 5 | Code lifetime and wrong guesses before the code is burned |

Fixed limits: one verification code per 60 seconds and 5 per hour per person; 3 test emails per hour per person; dispatch endpoint 30 requests per minute per caller.

## Run it locally

Console sender (nothing leaves the machine):

```bash
uv run python scripts/preview_emails.py var/email-preview      # every template with sample data
uv run python scripts/send_test_email.py persona@example.com   # one test email through the configured sender
uv run python scripts/dispatch_notifications.py                # send what is waiting in the outbox
```

Open the `.html` files in `var/email-preview/` in a browser. To try a real SMTP inbox (for example a sandbox that catches mail), set `EMAIL_PROVIDER=smtp` and the `EMAIL_SMTP_*` values, then run `send_test_email.py`. Use a sandbox, never a real customer address.

## Deploy checklist (when a provider and domain are chosen)

1. **Provider account and sending domain.** Created by the person who owns them; this repository never holds the account credentials.
2. **DNS records for the sending domain** (values come from the provider): SPF (`TXT`), DKIM (`CNAME` or `TXT`, one or more selectors) and DMARC (`TXT` at `_dmarc`, start with `p=none` and a report address, move to `quarantine` once reports are clean). Verify with the provider's checker and by sending a test email to a mailbox that shows authentication results.
3. **Secret Manager** entries, mounted as environment variables on Cloud Run: `FINANCE_AI_EMAIL_SMTP_PASSWORD`, `FINANCE_AI_NOTIFICATION_DISPATCH_TOKEN`, and the existing `FINANCE_AI_SESSION_LIMITER_SECRET`. Plain values (`EMAIL_FROM_ADDRESS`, `EMAIL_SMTP_HOST`, `PUBLIC_BASE_URL`, ...) can be ordinary environment variables.
4. **Cloud Scheduler job**: `POST https://<api>/internal/notifications/dispatch`, every minute, header `X-Dispatch-Token: <the token>`, a short timeout (30 s), no retries of its own (the outbox retries). A 200 response body reports `sent`, `skipped`, `retried`, `failed`.
5. **Migration 0021** applied before the new revision receives traffic (the container applies migrations on startup).
6. **Smoke test**: verify an address from the app, tap the test email button, trigger one budget alert with a small limit, confirm the unsubscribe link switches email off.

## Secrets and rotation

- **Dispatch token**: rotate by adding the new value to Secret Manager, updating the scheduler header and the service together. Wrong or missing tokens get 401 and the attempt is not logged with the value.
- **SMTP password**: rotate at the provider, update the secret, deploy. While wrong, sends fail with `provider_auth_failed` (not retryable, row `failed`): re-queue those rows after fixing it (see below).
- **`SESSION_LIMITER_SECRET`** also signs unsubscribe links and verification-code hashes. Rotating it invalidates open codes (they live minutes) and the unsubscribe links of emails already sent; people can still switch notifications off in the app.

## Reading the outbox (no addresses are stored in it)

```sql
-- what happened in the last day
SELECT kind, status, count(*) FROM notification_outbox
WHERE created_at > now() - interval '1 day' GROUP BY kind, status ORDER BY 1, 2;

-- why things were skipped or failed
SELECT last_error_code, count(*) FROM notification_outbox
WHERE status IN ('skipped', 'failed') GROUP BY 1 ORDER BY 2 DESC;

-- waiting right now (should be near zero when the scheduler runs every minute)
SELECT count(*), min(next_attempt_at) FROM notification_outbox WHERE status = 'pending';
```

Failure codes (retried only where marked): `provider_unavailable` (retried), `provider_error` (retried for temporary 4xx answers), `send_failed` (unexpected; retried), `provider_auth_failed`, `recipient_rejected`, `sender_rejected`, `invalid_recipient` and `invalid_header_*` (never retried). Skip reasons: `user_missing`, `user_inactive`, `email_not_verified`, `notifications_disabled`, `budget_alerts_disabled`, `daily_cap`, `superseded`.

To re-send rows that failed only because of a provider problem you have fixed: `UPDATE notification_outbox SET status = 'pending', attempts = 0, next_attempt_at = now(), last_error_code = NULL WHERE status = 'failed' AND last_error_code IN ('provider_auth_failed', 'provider_unavailable');`

## Privacy and security properties (each has a test)

- Addresses never appear in logs, the outbox, API errors or the app's diagnostics. The app only receives a masked hint (`a***@example.com`).
- The verification code is sent from the request and stored only as a keyed hash bound to the account. Codes live 15 minutes, allow 5 wrong guesses (counted exactly under concurrency) and work once.
- Header injection is refused (CR/LF in addresses, subjects or headers); templates autoescape; the unsubscribe page only shows a button on GET and changes state on POST, with no-store, noindex, no-referrer and a restrictive CSP.
- Unsubscribe tokens are versioned HMACs of the account number: forged, edited or foreign-secret tokens are refused and give no hint whether an account exists.
- Consent is checked again when sending, so turning email off, suspending an account or unverifying stops mail that was queued earlier.

## Not built yet

- **Bounce and complaint handling.** v1 stops retrying a rejected recipient (`recipient_rejected`) but has no suppression list or provider webhook. Add it with the provider chosen, because the event format is provider specific.
- **Metrics.** Counts come from the outbox queries above and the `notification.dispatch` log events; there is no dashboard.
- **v1.1 and v2** from the plan: ticket and suspension emails, monthly summary with PDF or Excel, and an on-device channel for the offline-first direction (the evaluator is separate from delivery for that reason).

## Open decisions

Provider (any SMTP-capable transactional service works with the current sender), sending domain and who owns the DNS, the production `FROM` address, and whether the monthly summary (v2) comes next.
