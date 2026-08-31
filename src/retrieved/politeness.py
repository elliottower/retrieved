"""How often this library is willing to ask a server for something.

Capturing only what an agent already fetched keeps the request ratio near one extra request per
human-initiated read, which is browser-shaped rather than crawler-shaped. That property is worth
protecting, and it is not automatic: an agent researching a topic can pull thirty pages from one
host in a minute, and thirty extra requests arriving in a burst is what gets an address blocked.

Every decision here is recorded rather than silent. A capture that did not happen because of a
rate limit is a fact about the record -- a store that keeps only successes cannot distinguish
"nothing to capture" from "capture declined", and that distinction is the point of the tool.

State lives in the library's SQLite index because each hook invocation is a separate process:
there is no memory between two fetches except what was written down.
"""

from __future__ import annotations

import datetime
import sqlite3
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

#: Minimum gap between two requests to one host, regardless of what robots.txt permits. A server
#: that publishes no Crawl-delay has not consented to being hit as fast as it can answer.
MIN_HOST_INTERVAL = 2.0

#: Captures per session. An agent that fetches more than this in one sitting is doing something
#: this tool was not built for, and the cap fails toward silence rather than toward a burst.
SESSION_CAP = 100

#: A URL fetched this recently is not fetched again. Re-reading a page an agent re-reads within
#: the same working session says nothing new and doubles the request count.
DEDUP_SECONDS = 900

#: robots.txt is re-read this often. Long enough not to be a request multiplier, short enough
#: that a newly added Disallow is honoured the same day.
ROBOTS_TTL = 86_400

SCHEMA = """
CREATE TABLE IF NOT EXISTS host_state (
    host        TEXT PRIMARY KEY,
    last_fetch  REAL,
    robots_body TEXT,
    robots_at   REAL
);
CREATE TABLE IF NOT EXISTS session_state (
    session_id  TEXT PRIMARY KEY,
    captures    INTEGER NOT NULL DEFAULT 0
);
"""


class Declined(Exception):
    """A capture this library chose not to make, carrying the reason for `skipped.jsonl`."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"declined {url}: {reason}")
        self.url = url
        self.reason = reason


def _now() -> float:
    return datetime.datetime.now(datetime.UTC).timestamp()


def _robots_for(
    db: sqlite3.Connection, host: str, scheme: str
) -> urllib.robotparser.RobotFileParser | None:
    """The host's robots.txt, cached. None where it could not be read.

    An unreadable robots.txt is not consent and is not refusal -- it is silence, and the
    convention is that silence permits. Recorded as such rather than treated as a Disallow,
    because refusing everything a host cannot serve robots for would capture almost nothing.
    """
    row = db.execute(
        "SELECT robots_body, robots_at FROM host_state WHERE host = ?", (host,)
    ).fetchone()
    body, fetched_at = row or (None, None)

    if body is None or not fetched_at or _now() - fetched_at > ROBOTS_TTL:
        try:
            response = httpx.get(
                f"{scheme}://{host}/robots.txt", timeout=10.0, follow_redirects=True
            )
            body = response.text if response.status_code == 200 else ""
        except httpx.HTTPError:
            body = ""
        db.execute(
            "INSERT INTO host_state (host, robots_body, robots_at) VALUES (?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET robots_body=excluded.robots_body, "
            "robots_at=excluded.robots_at",
            (host, body, _now()),
        )

    if not body:
        return None
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    return parser


def check(db: sqlite3.Connection, url: str, user_agent: str, session_id: str = "") -> None:
    """Raise `Declined` where this capture should not be made now. Silence means proceed.

    Called before the fetch and after the denylist, which answers a different question: the
    denylist is about what must never be requested, this is about what should not be requested
    yet, or again, or by this session.
    """
    db.executescript(SCHEMA)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()

    if session_id:
        row = db.execute(
            "SELECT captures FROM session_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row and row[0] >= SESSION_CAP:
            raise Declined(url, f"session cap of {SESSION_CAP} captures reached")

    recent = db.execute(
        "SELECT fetched_at FROM retrievals WHERE url = ? ORDER BY fetched_at DESC LIMIT 1", (url,)
    ).fetchone()
    if recent:
        try:
            when = datetime.datetime.fromisoformat(recent[0]).timestamp()
        except ValueError:
            when = 0.0
        if _now() - when < DEDUP_SECONDS:
            raise Declined(url, f"captured {int(_now() - when)}s ago")

    robots = _robots_for(db, host, parts.scheme or "https")
    if robots and not robots.can_fetch(user_agent, url):
        raise Declined(url, "robots.txt disallows this path")

    delay = MIN_HOST_INTERVAL
    if robots:
        published = robots.crawl_delay(user_agent)
        if published:
            delay = max(delay, float(published))

    row = db.execute("SELECT last_fetch FROM host_state WHERE host = ?", (host,)).fetchone()
    if row and row[0] and _now() - row[0] < delay:
        waited = _now() - row[0]
        raise Declined(url, f"{host} fetched {waited:.1f}s ago, minimum interval {delay:.0f}s")


def record(db: sqlite3.Connection, url: str, session_id: str = "") -> None:
    """Note that a request was made, so the next call to `check` can pace against it."""
    db.executescript(SCHEMA)
    host = (urlsplit(url).hostname or "").lower()
    db.execute(
        "INSERT INTO host_state (host, last_fetch) VALUES (?,?) "
        "ON CONFLICT(host) DO UPDATE SET last_fetch=excluded.last_fetch",
        (host, _now()),
    )
    if session_id:
        db.execute(
            "INSERT INTO session_state (session_id, captures) VALUES (?,1) "
            "ON CONFLICT(session_id) DO UPDATE SET captures = captures + 1",
            (session_id,),
        )
