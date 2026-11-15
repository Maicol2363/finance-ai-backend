# Code Generation Plan: CORS for Request Guard Failures

## Unit Context

- Unit: `cors-request-guard-failures`.
- Scope: place configured CORS handling outside `RequestGuardMiddleware` and verify early `413` and `415` responses.
- Contracts: preserve existing error payloads, status codes, correlation IDs, trusted-origin policy, and credential-free CORS behavior.
- No API schema, persistence, migration, or frontend changes.

## Steps

1. [x] Confirm the open PR branch is current and inspect middleware construction and existing CORS tests.
2. [x] Define the focused fix: register CORS after the request guard so Starlette wraps the guard with CORS.
3. [x] Reorder middleware registration in `src/finance_ai/main.py`.
4. [x] Add trusted-origin regression coverage for guard-generated `415` and `413` responses, including exposed correlation IDs.
5. [x] Run focused tests, formatting/lint checks, type checks, the full available suite, and `git diff --check`.
6. [x] Review the final diff and confirm only the intended application, test, and AI-DLC task files are changed.
7. [x] Publish the fix to the existing PR branch.

## Verification Results

- Focused CORS tests: 4 passed, including new early 413/415 response assertions.
- Full available suite: 383 passed, 65 PostgreSQL integration tests skipped because `FINANCE_AI_TEST_DATABASE_URL` is not configured.
- mypy: passed for 143 source files.
- Ruff format/check and `git diff --check`: passed.
- Aggregate local coverage: 79%; PostgreSQL tests were unavailable, so the repository's 80% aggregate target remains unverified in this checkout.
- PR #34 head: `919e43bd755687e5c60aebef3f44da1efaef16cc`; the GitHub `quality` check was pending at the last status read.
