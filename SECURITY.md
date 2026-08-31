# Security

## The shape of the risk

This package fetches URLs chosen by a language model. That is a server-side request forgery
primitive by construction: something else names the target, and this library makes the request
from your machine, with your network position and whatever credentials that implies.

`src/retrieved/refuse.py` is the boundary. It is checked before the request and again against the
URL redirects actually reached, because a shortener resolving to a private host defeats any check
made earlier.

Where a browser renders the page it is checked a third time, against wherever the browser
navigated. A page can move itself with `location =` after the HTTP response is complete, which is
a redirect the HTTP client's chain never sees, and the rendered bytes come from the address it
reached rather than the one that was requested.

Refused: cloud metadata endpoints, loopback, RFC 1918, link-local, reserved and multicast
addresses, hosts resolving to any of those, non-HTTP schemes, and URLs carrying a token,
credential or identifier in the query string.

`Set-Cookie`, `Cookie`, `Authorization` and `Proxy-Authorization` are dropped before a record is
written.

The rate limiter reads `robots.txt`, which is a second request to a URL built from the same
agent-chosen host, so it goes through the denylist as well. Every caller checks the target first;
the limiter checks its own regardless, because a request the boundary never sees is outside the
boundary no matter who made it.

CI runs a job that removes the guard and requires the test suite to go red. A denylist whose
tests pass without it is decoration.

## What the store contains

Whatever the pages contained, including anything adversarial: hidden elements, zero-width
characters, injected instructions. **Treat stored content as untrusted input.** Anything that
renders it — a viewer, a summariser, an agent reading a capture — must handle it accordingly.

This cuts the other way too. A prompt-injection incident is currently unreconstructable because
nobody keeps the page. A store that does is the only forensic record of what was served, with one
caveat that belongs in any such investigation: an attacker serving conditionally, on user agent,
address or timing, may show the agent an injected page and this library a clean one.

## Reporting

Open a GitHub security advisory on the repository. Do not open a public issue for a vulnerability
in the denylist.
