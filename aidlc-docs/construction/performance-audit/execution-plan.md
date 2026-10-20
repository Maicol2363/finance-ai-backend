# Backend Performance Audit Execution Plan

- [x] Identify the backend repository, preserve existing checkout changes, and create the requested worktree from `origin/develop`.
- [x] Inspect backend architecture, dependencies, tests, and performance-sensitive query paths.
- [x] Record candidates and choose only a code-backed optimization with a measurable database access path.
- [x] Add sargable local-time bounds to dashboard expense scans whose aggregates already consume bounded periods.
- [x] Run focused regression tests, static checks, and a synthetic PostgreSQL before/after benchmark.
- [x] Obtain read-only independent review and address findings, including DST boundaries and Decimal precision in assertions.
- [x] Summarize evidence, changes, limitations, and unverified candidates.
