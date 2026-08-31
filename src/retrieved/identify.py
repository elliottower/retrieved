"""Whether a retrieval is a work, and which one.

Most captures are not works. A docs page, a README, a status page and a blog post have no
authors, no year and no identifier, and inventing a bibliographic record for one produces an
entry keyed by a hash of its title -- the defect that fills a citation library with duplicates of
things it already holds.

A few captures are works, and say so plainly: a DOI in a meta tag, an arXiv id in the URL. Those
are worth recognising, and recognising is all this does. **Nothing here writes to a citation
library.** Identification is automatic and cheap; deciding a work belongs in a bibliography is a
judgement, and `retrieved promote` is where a person makes it.
"""

from __future__ import annotations

import re

#: Publishers, preprint servers and repositories all emit these, and they are the closest thing
#: the web has to a citation. Ordered by how much they are trusted when several disagree.
META_PATTERNS = (
    (
        "doi",
        re.compile(
            rb'<meta[^>]+name=["\'](?:citation_doi|DC\.Identifier\.DOI)["\'][^>]+content=["\']([^"\']+)',
            re.I,
        ),
    ),
    (
        "doi",
        re.compile(
            rb'<meta[^>]+content=["\'](10\.\d{4,9}/[^"\']+)["\'][^>]+name=["\']citation_doi', re.I
        ),
    ),
    (
        "arxiv",
        re.compile(rb'<meta[^>]+name=["\']citation_arxiv_id["\'][^>]+content=["\']([^"\']+)', re.I),
    ),
    (
        "title",
        re.compile(rb'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)', re.I),
    ),
)

#: An arXiv id is in the URL of every arXiv page, which is why it is worth reading there: it
#: identifies the work without parsing anything.
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|html|pdf)/(\d{4}\.\d{4,5})", re.I)

#: A DOI can appear in a URL too, on a publisher's landing page or a doi.org redirect.
DOI_URL = re.compile(r"doi\.org/(10\.\d{4,9}/[^\s?#]+)", re.I)


def identify(url: str, body: bytes = b"") -> dict[str, str]:
    """What identifies the work at this URL, or an empty dict where nothing does.

    The URL is read first because it cannot be wrong about itself: an arXiv id in an arXiv URL is
    the work's identifier by construction, where a meta tag is whatever the page chose to claim.
    """
    found: dict[str, str] = {}

    if match := ARXIV_URL.search(url):
        found["arxiv"] = match.group(1)
    if match := DOI_URL.search(url):
        found["doi"] = match.group(1).rstrip(").,;")

    for field, pattern in META_PATTERNS:
        if field in found:
            continue
        if match := pattern.search(body):
            value = match.group(1).decode("utf-8", errors="replace").strip()
            if value:
                found[field] = value

    # A DOI minted by arXiv is an arXiv id wearing a DOI. Recording both under their own names
    # would make one work look like two, which is the failure this module exists to avoid.
    doi = found.get("doi", "").lower()
    if doi.startswith("10.48550/arxiv."):
        found.setdefault("arxiv", doi.split("arxiv.")[-1])
        del found["doi"]

    return found


def slug_for(identifiers: dict[str, str]) -> str:
    """The slug a citation library would file this under, or "" where it would not file it.

    Deliberately the same rule the library uses: a DOI, else an arXiv id, else nothing. Returning
    "" rather than a title hash is the point -- a work this cannot identify is one that should
    stay a retrieval.
    """
    if doi := identifiers.get("doi"):
        return "doi-" + re.sub(r"[^a-z0-9]+", "-", doi.lower()).strip("-")
    if arxiv := identifiers.get("arxiv"):
        return "arxiv-" + arxiv.replace(".", "-")
    return ""
