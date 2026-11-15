"""Authorization role policy shared by the customer API and the staff portal.

The product has exactly four authorization roles. Free/Premium are plans, not roles.
Backend routes must enforce these sets; frontend navigation is presentation only.
"""

from typing import Final

CUSTOMER_ROLE: Final = "user"
SUPPORT_AGENT_ROLE: Final = "support_agent"
ADMIN_ROLE: Final = "admin"
SECURITY_AUDITOR_ROLE: Final = "security_auditor"

# Roles that may hold a staff browser session. Administrator portal access is not
# implemented yet, so admin remains denied until its stories are delivered.
STAFF_PORTAL_ROLES: Final[frozenset[str]] = frozenset({SUPPORT_AGENT_ROLE, SECURITY_AUDITOR_ROLE})

# SEC-01: privileged staff roles that need password plus TOTP before any privileged
# authority. Only the Security Auditor consumer is wired in this delivery.
MFA_REQUIRED_ROLES: Final[frozenset[str]] = frozenset({SECURITY_AUDITOR_ROLE})

# Staff-only identities that may not authenticate to the customer finance API.
CUSTOMER_API_DENIED_ROLES: Final[frozenset[str]] = frozenset({SECURITY_AUDITOR_ROLE})
