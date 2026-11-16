"""Authorization role policy shared by the customer API and the staff portal.

The product has exactly four authorization roles. Free/Premium are plans, not roles.
Backend routes must enforce these sets; frontend navigation is presentation only.
"""

from typing import Final

CUSTOMER_ROLE: Final = "user"
SUPPORT_AGENT_ROLE: Final = "support_agent"
ADMIN_ROLE: Final = "admin"
SECURITY_AUDITOR_ROLE: Final = "security_auditor"

# Roles that may hold a staff browser session. Administrators are provisioned by the
# controlled backend procedure (scripts/admin_ops.py) and always need MFA.
STAFF_PORTAL_ROLES: Final[frozenset[str]] = frozenset(
    {SUPPORT_AGENT_ROLE, SECURITY_AUDITOR_ROLE, ADMIN_ROLE}
)

# SEC-01: privileged staff roles that need password plus TOTP before any privileged
# authority, including sessions that were issued before the role was assigned.
MFA_REQUIRED_ROLES: Final[frozenset[str]] = frozenset({SECURITY_AUDITOR_ROLE, ADMIN_ROLE})

# Staff roles created through the ordinary Administrator portal (ADM-03/ADM-04). The admin
# role is never assigned or revoked through the portal or its API.
PORTAL_ASSIGNABLE_STAFF_ROLES: Final[frozenset[str]] = frozenset(
    {SUPPORT_AGENT_ROLE, SECURITY_AUDITOR_ROLE}
)

# Staff-only identities that may not authenticate to the customer finance API.
CUSTOMER_API_DENIED_ROLES: Final[frozenset[str]] = frozenset({SECURITY_AUDITOR_ROLE, ADMIN_ROLE})
