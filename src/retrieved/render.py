"""Fetch a page the way a browser would, when fetching it the way curl does returns nothing.

A growing share of the web serves an empty document and builds the page in JavaScript. An HTTP
client sees the shell: a few hundred bytes of markup, a script tag, and no text. Storing that is
not wrong -- those bytes really were served -- but it captures none of what the agent read, and a
record whose text digest covers an empty page answers no question anyone has.

This is an optional extra, and deliberately so. A headless browser is a few hundred megabytes and
a second per page; most captures do not need one, and a tool that requires it to be installed at
all is a tool most people will not install. Without it, nothing here runs and every record says
`javascript_executed: false`, which is true and is the honest thing to say.

    pip install "retrieved[browser]"
    python -m playwright install chromium
"""

from __future__ import annotations

import re

#: Below this much text, from a document that clearly is HTML, the page did not come with its
#: content. Chosen to be obviously a shell rather than merely a short page: a real short page --
#: a status endpoint, a stub -- still carries a sentence or two.
SHELL_TEXT_CHARS = 400

#: Roots that frameworks mount into. Their presence beside almost no text is what distinguishes
#: "the page is built by script" from "the page is short".
SHELL_MARKERS = re.compile(
    rb'id=["\'](?:root|app|__next|__nuxt|svelte|ember-app)["\']|<div[^>]+data-reactroot', re.I
)


def available() -> bool:
    """Whether a browser can be driven here. False is a normal state, not a broken install."""
    try:
        import playwright.sync_api  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def looks_like_a_shell(body: bytes, text: str, content_type: str) -> bool:
    """Whether this page probably needs a browser to say anything.

    Asked before spending a second and a browser process on it. Wrong in the cheap direction on
    purpose: rendering a page that did not need it costs time, and skipping one that did costs a
    capture that says nothing.
    """
    if "html" not in content_type.lower():
        return False
    if len(text) >= SHELL_TEXT_CHARS:
        return False
    return bool(SHELL_MARKERS.search(body)) or (len(body) > 0 and len(text) < 100)


def render(url: str, *, timeout: float = 20.0) -> tuple[bytes, str, str] | None:
    """The page after its scripts have run, as (bytes, rendering_method, final_url).

    None if no browser is installed or the render fails: a capture without JavaScript is worse
    than one with it and far better than none, so this degrades to the plain fetch rather than
    losing the page.

    The final URL is returned because the browser does its own navigating. A page can move itself
    with `location =` after the HTTP response is complete, so where the browser ended up is not
    something the HTTP client's redirect chain knows, and it is the address the stored bytes
    actually came from. The caller checks it against the denylist.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
                html = page.content()
                landed = page.url
                version = browser.version
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - any browser failure degrades to the plain fetch
        return None

    return html.encode("utf-8"), f"playwright chromium {version}", landed or url
