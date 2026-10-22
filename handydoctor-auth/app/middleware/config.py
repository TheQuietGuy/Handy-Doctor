"""
app/middleware/config.py — Rate-limit policy table.

Every route group defines:
  max_requests  — calls allowed within the window
  window_secs   — rolling window length in seconds
  scope         — human-readable label used in headers and logs

Route matching is longest-prefix: /api/accounts/register beats /api/accounts.
Unmatched paths fall through to DEFAULT.

Tuning rationale
────────────────
• auth_strict   — registration and password reset hit the email provider;
                  10 req / 15 min prevents inbox flooding from one IP.
• auth_standard — login and token endpoints; 20 req / 15 min is generous
                  enough for a human retrying but tight enough to slow bots.
                  (Per-email lockout in rate_limit.py adds a second layer.)
• account_write — sensitive mutations (password, email, deletion); low limit
                  because legitimate users rarely hit these repeatedly.
• account_read  — GET endpoints; high limit for normal browsing.
• default       — catch-all; permissive to avoid false positives.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RatePolicy:
    max_requests: int
    window_secs: int
    scope: str


# ── Policy table (matched longest-prefix first) ───────────────────────────────
ROUTE_POLICIES: list[tuple[str, RatePolicy]] = [
    # Strictest — hits the email provider
    ("/api/accounts/register", RatePolicy(10, 900, "auth_strict")),
    ("/api/accounts/forgot-password", RatePolicy(5, 900, "auth_strict")),
    ("/api/accounts/resend-verification", RatePolicy(5, 900, "auth_strict")),
    # Auth flow — two layers (this + per-email login lockout)
    ("/api/accounts/login", RatePolicy(20, 900, "auth_standard")),
    ("/api/accounts/token/refresh", RatePolicy(30, 900, "auth_standard")),
    ("/api/accounts/verify-email", RatePolicy(10, 900, "auth_standard")),
    ("/api/accounts/reset-password", RatePolicy(10, 900, "auth_standard")),
    # Sensitive account mutations
    ("/api/accounts/me/change-password", RatePolicy(10, 3600, "account_write")),
    ("/api/accounts/me/change-email", RatePolicy(10, 3600, "account_write")),
    ("/api/accounts/me/request-deletion", RatePolicy(5, 3600, "account_write")),
    ("/api/accounts/me/export", RatePolicy(5, 3600, "account_write")),
    # Read / light writes
    ("/api/accounts/me", RatePolicy(120, 60, "account_read")),
    ("/api/accounts/oauth", RatePolicy(20, 900, "auth_standard")),
    # Chat — message sending limited to prevent API abuse
    ("/api/chat/sessions", RatePolicy(60, 60, "chat")),

    # Admin — generous since it's a trusted, small user base, but still bounded
    ("/api/admin", RatePolicy(120, 60, "admin")),

    # Catch-all
    ("/", RatePolicy(200, 60, "default")),
]


def get_policy(path: str) -> RatePolicy:
    """Return the most specific matching policy for *path*."""
    for prefix, policy in ROUTE_POLICIES:
        if path.startswith(prefix):
            return policy
    return RatePolicy(200, 60, "default")
