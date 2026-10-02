"""Outbound URL safety (SSRF guard) for provider endpoints. Stdlib only.

Provider base URLs are admin-supplied but still untrusted input: a typo or a
hostile value must never let the server call its own private network or cloud
metadata service.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp")


class UnsafeUrl(ValueError):
    pass


def validate_provider_url(url: str, *, allow_private: bool = False, require_https: bool = True) -> str:
    """Return the normalised URL or raise UnsafeUrl."""
    if not url or not isinstance(url, str):
        raise UnsafeUrl("URL is required")
    url = url.strip()
    if len(url) > 512:
        raise UnsafeUrl("URL is too long")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise UnsafeUrl("URL must start with https://")
    if require_https and parsed.scheme != "https" and not allow_private:
        raise UnsafeUrl("URL must use https://")
    if parsed.username or parsed.password:
        raise UnsafeUrl("Credentials in the URL are not allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUrl("URL has no host")
    if allow_private:
        return url
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrl("Internal hostnames are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise UnsafeUrl("Private or reserved IP addresses are not allowed")
    elif "." not in host:
        raise UnsafeUrl("Host must be a fully-qualified domain name")
    return url
