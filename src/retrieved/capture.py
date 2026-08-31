"""Fetch a URL independently and keep what came back.

Independently is the whole point. An agent's fetch tool returns a model's answer about a page, not
the page -- so a harness that recorded what the agent reported would be recording a summary and
calling it a source. This makes its own request, and stores bytes obtained by something with no
stake in the claim.

What it cannot promise, and says so in every record: that these are the bytes the agent saw. A
re-fetch can differ -- dynamic pages, personalisation, a paywall, time. The honest claim is that
this content was at this URL at this moment, which is weaker and checkable.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, field

import httpx

from retrieved.refuse import Refused, check, check_final

#: Sent so an operator can see who is asking and object. A capture tool with an anonymous
#: user-agent is indistinguishable from a scraper, and deserves to be treated as one.
USER_AGENT = "retrieved/0.1 (+https://github.com/elliottower/retrieved)"

#: Never stored. A record carrying these is a credential store wearing an evidence store's name.
SECRET_HEADERS = frozenset({"set-cookie", "authorization", "proxy-authorization", "cookie"})

#: Past this, store the digest and the first megabyte rather than the whole thing. A capture tool
#: that fills a disk gets uninstalled, and an enormous response is rarely the evidence anyway.
MAX_BYTES = 8 * 1024 * 1024

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_SPACE = re.compile(r"\s+")


@dataclass
class Retrieval:
    """One fetch, and what can honestly be said about it."""

    url: str
    final_url: str
    fetched_at: str
    http_status: int
    content_type: str
    bytes_len: int
    bytes_sha256: str
    text_sha256: str
    extractor: str
    rendering_method: str
    javascript_executed: bool
    response_headers: dict[str, str]
    body: bytes = field(repr=False, default=b"")
    truncated: bool = False
    note: str = ""


def normalize(body: bytes, content_type: str) -> str:
    """The text a digest is taken over, stripped of what changes on every fetch.

    Bytes move constantly -- session ids, CSRF tokens, ad slots, timestamps -- so a byte digest
    answers "are these the same bytes" and almost always says no. This answers the question worth
    asking: does the page still say the same thing.
    """
    text = body.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        text = _SCRIPT.sub(" ", text)
        text = _TAG.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def fetch(url: str, *, timeout: float = 30.0, client: httpx.Client | None = None) -> Retrieval:
    """Fetch `url`, or raise `Refused` if it must not be fetched.

    Checked twice: before the request, and against the URL redirects actually reached. The second
    is the one that catches a shortener pointing at a private host, and it is the check a first
    version forgets.
    """
    check(url)

    owned = client is None
    client = client or httpx.Client(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    try:
        response = client.get(url)
    finally:
        if owned:
            client.close()

    final_url = str(response.url)
    if final_url != url:
        check_final(url, final_url)

    body = response.content
    truncated = len(body) > MAX_BYTES
    if truncated:
        body = body[:MAX_BYTES]

    content_type = response.headers.get("content-type", "")
    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() not in SECRET_HEADERS}

    return Retrieval(
        url=url,
        final_url=final_url,
        fetched_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        http_status=response.status_code,
        content_type=content_type,
        bytes_len=len(body),
        bytes_sha256=hashlib.sha256(body).hexdigest(),
        text_sha256=hashlib.sha256(normalize(body, content_type).encode()).hexdigest(),
        extractor=f"retrieved.normalize/{'html' if 'html' in content_type.lower() else 'raw'}",
        # No browser here, so nothing scripted ran. Recorded rather than assumed, because a page
        # that is a JavaScript shell reads as an empty page and the two must be distinguishable.
        rendering_method="httpx, no browser",
        javascript_executed=False,
        response_headers=headers,
        body=body,
        truncated=truncated,
    )


__all__ = ["Refused", "Retrieval", "fetch", "normalize"]
