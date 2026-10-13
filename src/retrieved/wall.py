"""Recognise a page that stands in front of the page asked for.

A server that doubts a visitor often answers with success all the same: a check on the browser,
a request it rejected, a demand for cookies or a sign-in, each served as `200 OK`. The status
says the page arrived and the page says it did not. Stored without a word, such a retrieval
reads as a capture of the article, and an answer checked against it reports every quotation
missing from a page nobody quoted.

Design constraints, in order:

1. Named, never guessed. A page is a wall where it says one of the phrases below, or where the
   server's own header says it challenged the request. A page that is merely short is not one:
   a stub, a redirect notice and a shell no script filled are short for other reasons, and
   they keep the reason they had.
2. Short pages only. The phrases are ordinary words, and an article about bot detection uses all
   of them. A wall says little else, so nothing holding WALL_TEXT_CHARS of text or more is
   called one whatever it says.
3. Recorded, never worked around. Nothing here retries, waits or presents itself as a browser.
   A wall is a server declining, and the record says that it did.

Run over 1,082 stored pages served with success as HTML or text, from two months of ordinary
work, this named 70. Each was a wall on reading: 69 held under 400 characters of text and one,
a sign-in page, 991. Of the 36 other pages under 400 characters none was a wall. They were
shells no script had filled, stubs, empty documents and error pages served with success. The
pages of 2,000 characters or more that held "captcha", "access denied" or "log in to continue"
were all articles using the words.
"""

from __future__ import annotations

import html
import re

from retrieved.capture import Retrieval, normalize

#: At or above this much text a page is not called a wall, whatever phrases it holds.
WALL_TEXT_CHARS = 2000

_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_SPACE = re.compile(r"\s+")
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'"})

# What kind of wall, and the words that say so, compared after case folding. The kinds are kept
# apart because they are different facts: a check may pass for a browser, a rejection is the
# server's decision, and a sign-in page is the page for everyone who has not signed in.
_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "a check on the browser",
        (
            "checking your browser",
            "verifying your browser",
            "verification required",
            "making sure you're not a bot",
            "verify you are human",
            "verify you are a human",
            "verifying you are human",
            "are you a robot",
            "client challenge",
            "bot manager captcha",
            "pardon our interruption",
            "unusual traffic",
            "attention required",
            "enable javascript and cookies to continue",
            "just a moment...",
        ),
    ),
    (
        "a request the server rejected",
        (
            "request rejected",
            "request unsuccessful",
            "access denied",
            "access to this page has been denied",
        ),
    ),
    ("a demand for cookies", ("cookies must be enabled",)),
    ("a sign-in page", ("sign in to continue", "log in to continue")),
)

#: In a page's title and nowhere else. An article mentions a CAPTCHA; a page titled as one is one.
_TITLE_ONLY = ("captcha",)


def _said(text: str) -> str:
    return _SPACE.sub(" ", html.unescape(text)).translate(_APOSTROPHES).casefold()


def recognise(retrieval: Retrieval) -> str:
    """What kind of wall a retrieval is and the words that said so, or "" where it is none.

    The words returned are this module's own phrase and never the page's text, so what is
    written to a record from here is nothing a page chose.
    """
    if retrieval.response_headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return "a check on the browser (the response header `cf-mitigated: challenge`)"
    if "html" not in retrieval.content_type.lower():
        return ""
    text = normalize(retrieval.body, retrieval.content_type)
    if len(text) >= WALL_TEXT_CHARS:
        return ""
    said = _said(text)
    for kind, phrases in _PHRASES:
        for phrase in phrases:
            if phrase in said:
                return f'{kind} (the page says "{phrase}")'
    title = _TITLE.search(retrieval.body.decode("utf-8", errors="replace"))
    if title:
        for phrase in _TITLE_ONLY:
            if phrase in _said(title.group(1)):
                return f'a check on the browser (the page is titled with "{phrase}")'
    return ""


__all__ = ["WALL_TEXT_CHARS", "recognise"]
