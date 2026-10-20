# Requirements: Transfer Concurrency and Idempotent Replay

## Intent

Fix only confirmed concurrency failures in the existing transfer module while preserving all observable API behavior and ledger semantics.

## Functional Requirements

- Concurrent transfers touching the same two accounts must acquire row locks in a consistent account-ID order to avoid cross-lock deadlocks.
- Concurrent confirmations with the same owner and proposal ID must return the committed transfer as an idempotent replay after the losing transaction rolls back.
- The uniqueness guarantee must continue preventing duplicate transactions and movements.
- Account ownership, active-state, currency, amount, and transfer rules must remain unchanged.

## Compatibility and Security Requirements

- Keep the HTTP path, status, request/response schemas, OpenAPI contract, authentication, and persisted schema unchanged.
- Preserve owner scoping and never return another owner's transaction.
- Preserve source-negative/destination-positive ledger entries and exact decimal values.
- No frontend or Android changes are needed because public response contracts do not change.

## Acceptance Criteria

- PostgreSQL integration coverage verifies opposite-direction concurrent transfers complete without partial ledger state.
- PostgreSQL integration coverage synchronizes duplicate proposal requests after both initial replay lookups miss; exactly one transaction is stored and both callers receive the same transfer, with one marked replay.
- Existing focused transfer tests and applicable lint/type checks pass.
