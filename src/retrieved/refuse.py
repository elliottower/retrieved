"""What must never be fetched.

Re-fetching a URL an agent chose is a server-side request forgery primitive: the agent picks the
target and this library makes the request, from a machine that may hold cloud credentials. So the
denylist is not hygiene, it is the security boundary, and it is checked twice -- once on the URL
requested and once on the URL a redirect actually reached, because a shortener resolving to a
private host defeats any check made before the request.

The rule is deny-by-default on anything that is not plainly a public web page.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

#: Only these reach the network. `file:`, `data:` and extension schemes are not retrievals in any
#: sense this records, and each has been a real exfiltration route in some other tool.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Cloud instance metadata. A re-fetch of this writes credentials to disk as evidence, which is
#: the worst outcome this whole library could produce.
METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS, GCP, Azure, DigitalOcean
        "metadata.google.internal",
        "metadata.goog",
        "100.100.200.200",  # Alibaba
    }
)

#: Host suffixes that name a private network by convention rather than by address.
PRIVATE_SUFFIXES = ("localhost", ".local", ".internal", ".localdomain", ".home.arpa")

#: Query parameters whose value is a secret or an identifier. The record outlives the secret, so a
#: URL carrying one is refused rather than stored with the value stripped -- a redacted URL is not
#: the URL that was fetched, and recording it as though it were would be a small lie.
SECRET_PARAMS = re.compile(
    r"(?:^|[?&])(?:"
    r"access_token|auth|api_?key|token|session|sessionid|sid|password|passwd|pwd|secret|"
    r"signature|sig|code|state|id_token|refresh_token|client_secret|email|patient|mrn"
    r")=",
    re.I,
)


class Refused(Exception):
    """A URL this library will not fetch, carrying the reason for the record."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"refused {url}: {reason}")
        self.url = url
        self.reason = reason


def _is_private(host: str) -> bool:
    """True where the host names a non-public address, resolving it if it is a name.

    Resolution is the point. `evil.example.com` with an A record of 127.0.0.1 is a private target
    wearing a public name, and only asking the resolver can tell.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False  # unresolvable is not private; the fetch will fail on its own
        addresses = {ipaddress.ip_address(i[4][0]) for i in infos}
        return any(_blocked_address(a) for a in addresses)
    return _blocked_address(address)


def _blocked_address(address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def check(url: str) -> None:
    """Raise `Refused` where this URL must not be fetched. Silence means it may be."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    if scheme not in ALLOWED_SCHEMES:
        raise Refused(url, f"scheme {scheme or '(none)'} is not http or https")

    host = (parts.hostname or "").lower()
    if not host:
        raise Refused(url, "no host")

    if host in METADATA_HOSTS:
        raise Refused(url, "cloud instance metadata endpoint")

    if host == "localhost" or any(host.endswith(s) for s in PRIVATE_SUFFIXES):
        raise Refused(url, f"{host} names a private network")

    if _is_private(host):
        raise Refused(url, f"{host} resolves to a non-public address")

    if SECRET_PARAMS.search(parts.query or ""):
        raise Refused(url, "query string carries a token, credential or identifier")


def check_final(requested: str, final: str) -> None:
    """Check again after redirects.

    The first check cannot see where a shortener points. This is the one that matters, and the
    message names both URLs because a redirect into a private network is worth reading twice.
    """
    try:
        check(final)
    except Refused as e:
        raise Refused(final, f"redirected from {requested}: {e.reason}") from None
