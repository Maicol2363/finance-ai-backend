# Cross-repository staff foundation: schema mapping

**Status:** development foundation only; no business workflow or production route is implemented.
**Source of truth:** NullCorvus/finance-ai backlog and approved Support, Administrator and shared-security specifications. Security Auditor stories supplied for review remain provisional until finalized; the event table is a generic store and does not freeze query or report behavior.

| Requirement | Existing persistence | Foundation adaptation | Explicit boundary |
|---|---|---|---|
| Four roles | role and app_user.role_id; seeds currently contain user and admin | Seed support_agent and security_auditor idempotently; migration safely adds only missing role rows | No extra role, role grants, admin bootstrap or account changes |
| Account suspension | app_user.is_active and revocable user_session records | Reuse the account status; add MFA assurance timestamp to sessions only | Session revocation, last-admin concurrency and access denial remain future service work |
| Support tickets | No ticket tables | Add ticket, customer-visible message and operational activity tables; constrain the four approved states | No endpoints/transition services. Store escalation/return reasons on activity as restricted metadata; resolution summary on ticket. Enforce visibility in future backend projections |
| Staff invitation | No invitation model | Persist normalized email, approved staff role, SHA-256 token hash, creator, expiry and single-use consumption/activation linkage | No raw token, invitation delivery, activation flow or admin role grants |
| Administrator TOTP | No factor/session-MFA persistence | Store encrypted-secret bytes, factor state, verified time and accepted time-step; mark session MFA assurance time | Encryption key is external configuration; no key, OTP, recovery code or authentication flow is created here. Auditor MFA is undecided |
| Shared security audit | No security event table | Append-only event rows with actor/target identifiers, UTC occurrence, type/result, correlation ID and allowlisted JSON context; indexes support common filters and chronology | Generic event names permit future events. No history backfill, event emission, readers, analytics or PDF report storage. audit.report_exported requires finalized Auditor requirements |
| Auditor query/report | No audit reader | Reuse the shared event table as a source for future authorized reads and aggregation | No Auditor API/UI or arbitrary dashboard/report assumptions in this foundation |

## Implementation choices

Existing identity, role and opaque database-backed session models remain authoritative. New role seeds are name-based and idempotent; role IDs are never assumed. Legacy sessions receive a null MFA verification timestamp and no inferred assurance. Role changes and administrative elevation must explicitly revoke or revalidate sessions in later workflow code.

Ticket ownership and actors reference existing accounts; the activity table stores before/after status and assignee plus a separate internal reason field. Messages remain a separate customer-visible table. These columns support approved handoff/history without creating a private-message or general note system. Atomic return requires a later application transaction that updates the ticket and adds activity in the same unit of work.

The audit event actor and target are historical identifiers rather than cascading foreign keys, preserving event context if an account or target disappears. Event context is JSON for future allowlisted metadata; field filtering belongs in the event writer/service, not a UI. PostgreSQL blocks audit-row UPDATE/DELETE through a trigger; the downgrade refuses to drop a populated event table. Application database-role separation and operational retention/alert configuration remain deployment work. The migration creates no fabricated events.

## Administrator extension (migration 0020)

| Requirement | Persistence | Notes |
|---|---|---|
| Account suspension history | `account_suspension` (one open row per account) and `app_user.is_active` | Written only by procedure `sp_admin_set_account_suspension` together with its SEC-02 event |
| Last active administrator | Trigger `app_user_last_admin_guard` on `app_user` | Serialized with an advisory lock; fail-closed outside READ COMMITTED |
| Session revocation | Trigger `app_user_revoke_sessions_on_authority_change` and `user_session.issued_role_id` | Reactivation never restores sessions |
| MFA recovery | `staff_mfa_recovery_code` (hashes only) | Shown once at issue time |
| Escalated-ticket ownership and return | `support_ticket.admin_owner_user_id` + CHECK, procedure `sp_admin_return_escalated_ticket` | Status, assignment, ownership and activity change together |

Executable definitions and rationale: the migration file and `NullCorvus/finance-ai/aidlc-docs/construction/administrator/code/DB-ROUTINES-ADMIN.md`.

## Unresolved decisions kept for later

- Final Auditor query scope, date/time-zone presentation, actor/target lookup behavior, and report content/limits must follow the finalized AUD stories.
- Resolved by the approved SEC-01/AUD stories: security_auditor requires password plus TOTP (implemented for the Auditor with migration 0019's short-lived challenge table; one active authenticator per account). Multiple authenticators per administrator and Administrator MFA wiring remain open.
- Invitation and TOTP lifetimes, identity recovery, secret-key provisioning, exact text limits, ticket category list and audit failure transaction policy are future technical-design or story decisions.
- Browser authentication remains disabled until a secure browser session contract is implemented. The API client does not store credentials or invent endpoints.

## Local verification boundary

Migrations and integration tests must use an isolated database whose name ends in _test. No production or shared Neon database is migrated. The frontend staff shell starts independently with Vite; the existing Flutter app and the private PostgreSQL network remain unchanged.
