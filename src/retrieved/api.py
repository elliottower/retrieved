"""One call that captures a URL the way this library means it to be captured.

The command and the hook each walk the same steps: the denylist, the rate limiter, the request,
the record. A program that imports this library had to walk them itself, and the order is not
decoration. Asked in the wrong one, a URL that will never be fetched spends a slot against a
session's cap; left out, the limiter is not a limit. This is those steps once, for a caller that
is not either of them.

    from retrieved.api import capture

    captured = capture("https://example.org/paper")
    captured.retrieval.bytes_sha256, captured.record, captured.wall

What it raises is what the steps raise: `Refused` for a URL that must never be requested,
`Declined` for one that should not be requested now. Both are written to `skipped.jsonl` first,
because a capture that did not happen is a fact about the library whoever asked for it. An
error of the HTTP client's own is recorded there too, and then raised as it was.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from retrieved import politeness
from retrieved.capture import USER_AGENT, Retrieval, fetch
from retrieved.politeness import Declined
from retrieved.refuse import Refused
from retrieved.refuse import check as denylist
from retrieved.store import Library
from retrieved.wall import recognise


@dataclass(frozen=True)
class Captured:
    """One capture: what came back, where its record is, and whether it was the page."""

    retrieval: Retrieval
    record: pathlib.Path
    #: What kind of wall the server answered with, or "" where the page is not one. A status
    #: of 200 does not say, so a caller that means to quote the page reads this first.
    wall: str = ""


def capture(
    url: str,
    *,
    library: Library | None = None,
    prompt: str = "",
    session_id: str = "",
    pace: bool = True,
    wait: float = 0.0,
) -> Captured:
    """Fetch `url` and keep it in `library`, or raise `Refused` or `Declined`.

    `library` defaults to the one `Library.resolve` finds from here. `pace` false skips the
    rate limiter and never the denylist, which is what `retrieved capture --now` does. `wait`
    is how long the caller will be held for a host's interval before the request is declined.
    """
    library = (library or Library.resolve()).create()
    # The denylist first, and pacing second. They answer different questions -- what must never
    # be requested, then what should not be requested yet -- and in the other order a URL that
    # will never be fetched still spends a slot against the session cap.
    try:
        denylist(url)
        if pace:
            with library.connect() as db:
                politeness.check(db, url, USER_AGENT, session_id=session_id, wait=wait)
        with library.connect() as db:
            politeness.record(db, url, session_id=session_id)
        # Checked again inside `fetch`, against the URL redirects actually reached.
        retrieval = fetch(url)
    except (Refused, Declined) as refusal:
        library.skip(refusal.url, refusal.reason, session_id=session_id)
        raise
    except Exception as error:
        library.skip(url, f"fetch failed: {type(error).__name__}", session_id=session_id)
        raise
    path = library.write(retrieval, session_id=session_id, prompt=prompt)
    return Captured(retrieval, path, recognise(retrieval))


__all__ = ["Captured", "Declined", "Refused", "capture"]
