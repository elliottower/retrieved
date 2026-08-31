"""Move a retrieval into a citation library, when a person decides it belongs there.

The line this draws is the whole design. Capture is automatic and indiscriminate: an agent fetches
a page and the bytes are kept, whatever the page was. A citation library is curated, and its
records are things someone chose to cite. Letting captures flow into it automatically would fill
it with docs pages and status endpoints, and the cost is not untidiness -- an entry with no
identifier is filed under a hash of its title, so the same work arrives again under a different
slug the next time anything cites it properly.

So promotion is a command, never a consequence. It refuses a retrieval it cannot identify, it
refuses to overwrite a record that already exists, and it copies the pinned bytes across so the
library holds the artifact rather than a link to one.
"""

from __future__ import annotations

import pathlib
import shutil

import yaml

from retrieved.identify import identify, slug_for
from retrieved.store import Library


class CannotPromote(Exception):
    """Why this retrieval is not going into a bibliography."""


def promote(
    library: Library, digest: str, citations_home: pathlib.Path, *, force: bool = False
) -> pathlib.Path:
    """Write a bibliographic record for one retrieval, and copy its bytes into the library.

    Returns the record's path. Raises `CannotPromote` rather than guessing: a work this cannot
    name is one that should stay a retrieval, and saying so is more useful than filing it under a
    title hash where it will be duplicated later.
    """
    # A prefix, because `promotable` prints sixteen characters and asking for sixty-four back is
    # a tool disagreeing with itself. Ambiguity is reported rather than resolved by taking the
    # first: two retrievals sharing a prefix is a thing the caller should see.
    record_path = library.retrievals / f"{digest}.yaml"
    if not record_path.is_file():
        matches = sorted(library.retrievals.glob(f"{digest}*.yaml"))
        if len(matches) > 1:
            raise CannotPromote(
                f"{digest} matches {len(matches)} retrievals; use more of the digest"
            )
        if not matches:
            raise CannotPromote(f"no retrieval {digest[:16]} in {library.root}")
        record_path = matches[0]
    digest = record_path.stem

    retrieval = yaml.safe_load(record_path.read_text())
    blob = library.store / digest
    if not blob.is_file():
        raise CannotPromote(f"the bytes for {digest[:16]} are gone; only the record remains")

    identifiers = identify(retrieval["final_url"], blob.read_bytes())
    slug = slug_for(identifiers)
    if not slug:
        raise CannotPromote(
            f"{retrieval['final_url']} carries no DOI or arXiv id. A record with neither is filed "
            f"under a hash of its title and duplicates the moment anything cites it properly, so "
            f"this stays a retrieval."
        )

    records = citations_home / "records"
    records.mkdir(parents=True, exist_ok=True)
    target = records / f"{slug}.yaml"
    if target.exists() and not force:
        raise CannotPromote(f"{slug} is already in {citations_home}; --force to overwrite")

    # The bytes go with the record. A library holding a record whose artifact lives somewhere else
    # is a library of links, and a link is what content addressing exists to replace.
    store = citations_home / "store"
    store.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if "pdf" in (retrieval.get("content_type") or "") else ".html"
    shutil.copy2(blob, store / f"{slug}{suffix}")

    target.write_text(
        yaml.safe_dump(
            {
                "slug": slug,
                # Left blank rather than guessed. A title scraped from a meta tag is often the
                # page's title and not the work's, and a wrong author list beside a real
                # identifier is the failure `citations audit` exists to catch.
                "title": identifiers.get("title", ""),
                "authors": [],
                "year": "",
                "venue": "",
                "doi": identifiers.get("doi", ""),
                "arxiv": identifiers.get("arxiv", ""),
                "url": retrieval["final_url"],
                "local": f"store/{slug}{suffix}",
                "sha256": digest,
                "note": (
                    f"Promoted from a retrieval captured {retrieval['fetched_at']}. Metadata is "
                    f"from the page itself; run `citations resolve` to fill authors and year from "
                    f"a registry before citing it."
                ),
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )
    return target


def candidates(library: Library) -> list[tuple[str, str, str]]:
    """Retrievals that could be promoted: digest, slug, url. Everything else stays put."""
    found = []
    for record_path in sorted(library.retrievals.glob("*.yaml")):
        blob = library.store / record_path.stem
        if not blob.is_file():
            continue
        retrieval = yaml.safe_load(record_path.read_text())
        slug = slug_for(identify(retrieval["final_url"], blob.read_bytes()))
        if slug:
            found.append((record_path.stem, slug, retrieval["final_url"]))
    return found
