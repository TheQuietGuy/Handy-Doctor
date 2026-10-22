"""
app/middleware/ip.py — Client IP extraction.

In production HandyDoctor sits behind a reverse proxy (nginx / Cloudflare).
The real client IP arrives in X-Forwarded-For or CF-Connecting-IP.
We only trust those headers when TRUST_PROXY is enabled in settings — if
we trusted them unconditionally, any client could spoof their IP by injecting
the header themselves.

IPv6 addresses are normalised to their compressed form so
"::ffff:192.0.2.1" and "192.0.2.1" produce the same rate-limit key.
"""

import ipaddress
import logging

from starlette.requests import Request

from app.config import settings

logger = logging.getLogger(__name__)

# Headers checked in priority order when trust_proxy=True
_PROXY_HEADERS = [
    "CF-Connecting-IP",  # Cloudflare — single trusted IP
    "X-Real-IP",  # nginx proxy_set_header
    "X-Forwarded-For",  # standard; may be a comma-separated list
]


def extract_client_ip(request: Request) -> str:
    """
    Return the best-guess client IP as a normalised string.
    Falls back to the direct connection address if no proxy header is
    present or if proxy trust is disabled.
    """
    if getattr(settings, "trust_proxy", False):
        for header in _PROXY_HEADERS:
            value = request.headers.get(header)
            if value:
                # X-Forwarded-For can be "client, proxy1, proxy2" — take leftmost
                raw_ip = value.split(",")[0].strip()
                normalised = _normalise(raw_ip)
                if normalised:
                    return normalised

    # Direct connection (local dev, tests, or trust_proxy=False)
    direct = getattr(request.client, "host", "127.0.0.1") or "127.0.0.1"
    return _normalise(direct) or direct


def _normalise(raw: str) -> str | None:
    """
    Parse *raw* as an IP address and return its compressed string form.
    Returns None if *raw* is not a valid IP (e.g. a hostname).
    IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) are unwrapped to plain IPv4.
    """
    try:
        addr = ipaddress.ip_address(raw)
        # Unwrap IPv4-mapped IPv6 so both forms share the same key
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(addr)
    except ValueError:
        logger.debug("Could not parse IP address: %r", raw)
        return None
