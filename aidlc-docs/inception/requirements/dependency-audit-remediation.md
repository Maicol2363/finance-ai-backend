# Requirements: Dependency Audit Remediation

## Intent

Resolve the failing dependency audit in backend PR #33 by updating the two locked packages that the latest CI run flagged.

## Request Analysis

- Request type: dependency remediation.
- Scope: one package lockfile and task documentation.
- Complexity and risk: simple and low; changes are reversible.
- User impact: no direct feature or API behavior change; this is a dependency security update.

## Requirements

- Resolve dnspython from 2.8.0 to at least 2.9.0.
- Resolve msgpack from 1.2.2 to at least 1.2.3.
- Keep dependencies sourced from the existing official PyPI registry and retain lockfile hashes.
- The existing CI command uv run pip-audit must report no known vulnerabilities.
- Do not change application code, API contracts, database schema, or migrations.
- Apply the update on develop so the open release PR receives it.
- Preserve unrelated local untracked files.

## Verification

- Confirm the lockfile resolves with the repository's CI-pinned uv version.
- Run the dependency audit that currently fails in CI.
- Review the final diff before committing and pushing.
- GitHub Actions will run the full PR checks after the push.

## Extension Configuration

- Security Baseline: enabled. SECURITY-10 Software Supply Chain Security applies.
- Property-Based Testing: partial; N/A because this change adds no business logic or serialization behavior.
- Resiliency Baseline: disabled; skip and record in the audit log.
