"""Where retrievals live on disk.

A retrieval is not a bibliographic entry. A fetched page has no authors, no year and no DOI, and
forcing one into a bibliography's record shape produces an entry keyed by a hash of its title with
no identifier at all -- which is how a citation library ends up with hundreds of records for
works it already holds. So retrievals get their own store, and share only the part that was
already right: bytes addressed by their digest.

    <library>/
        store/<bytes_sha256>          the bytes
        retrievals/<bytes_sha256>.yaml the record
        index.db                      one row per retrieval, so questions are queries
        skipped.jsonl                 what was not captured, and why

`skipped.jsonl` is not an afterthought. A capture refused by the denylist or a rate limit is a
thing that did not happen, and a store that records only successes reports the same silence for
"nothing to capture" and "capture declined".
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
from collections.abc import Iterator

import yaml

from retrieved.capture import Retrieval

DEFAULT_DIRNAME = ".retrieved"

SCHEMA = """
CREATE TABLE IF NOT EXISTS retrievals (
    bytes_sha256 TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    final_url    TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER NOT NULL,
    text_sha256  TEXT NOT NULL,
    session_id   TEXT
);
CREATE INDEX IF NOT EXISTS retrievals_url ON retrievals(url);
CREATE INDEX IF NOT EXISTS retrievals_text ON retrievals(text_sha256);
"""


class Library:
    """A directory of retrievals, resolved the way a citations library is.

    `$RETRIEVED_HOME`, else a `.retrieved/` found by walking up from here, else one created in the
    current directory. The walk matters: a capture made from a subdirectory of a project belongs
    to that project's library, not to a new one beside wherever the agent happened to be.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.store = root / "store"
        self.retrievals = root / "retrievals"
        self.index_path = root / "index.db"
        self.skipped_path = root / "skipped.jsonl"

    @classmethod
    def resolve(cls, start: pathlib.Path | None = None) -> Library:
        if home := os.environ.get("RETRIEVED_HOME"):
            return cls(pathlib.Path(home).expanduser().resolve())
        here = (start or pathlib.Path.cwd()).resolve()
        for directory in [here, *here.parents]:
            candidate = directory / DEFAULT_DIRNAME
            if candidate.is_dir():
                return cls(candidate)
        return cls(here / DEFAULT_DIRNAME)

    def create(self) -> Library:
        self.store.mkdir(parents=True, exist_ok=True)
        self.retrievals.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        return self

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """The index, open for the length of one block.

        Commits on the way out, and closes. `with sqlite3.connect(...) as db` does only the
        first -- it is a transaction manager wearing the shape of a resource manager -- so code
        that looks like it releases the handle keeps it until the object is collected. In a hook
        that is a warning; on a filesystem that locks, it is a second process that cannot write.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index_path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def write(
        self, retrieval: Retrieval, *, session_id: str = "", prompt: str = ""
    ) -> pathlib.Path:
        """Store the bytes and the record, and index the row. Returns the record's path."""
        self.create()
        (self.store / retrieval.bytes_sha256).write_bytes(retrieval.body)

        record = {
            "url": retrieval.url,
            "final_url": retrieval.final_url,
            "fetched_at": retrieval.fetched_at,
            "http_status": retrieval.http_status,
            "content_type": retrieval.content_type,
            "bytes": retrieval.bytes_len,
            "bytes_sha256": retrieval.bytes_sha256,
            "text_sha256": retrieval.text_sha256,
            "extractor": retrieval.extractor,
            "rendering_method": retrieval.rendering_method,
            "javascript_executed": retrieval.javascript_executed,
            "response_headers": retrieval.response_headers,
            "truncated": retrieval.truncated,
            # Recorded because it is what the agent was asking of the page -- the closest thing to
            # intent available for free. It is not a quotation and must never be read as one.
            "prompt": prompt,
            "session_id": session_id,
            "note": (
                "An independent retrieval. These are the bytes this library fetched at "
                "fetched_at, not necessarily the bytes any agent saw."
            ),
        }
        path = self.retrievals / f"{retrieval.bytes_sha256}.yaml"
        path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))

        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO retrievals VALUES (?,?,?,?,?,?,?)",
                (
                    retrieval.bytes_sha256,
                    retrieval.url,
                    retrieval.final_url,
                    retrieval.fetched_at,
                    retrieval.http_status,
                    retrieval.text_sha256,
                    session_id,
                ),
            )
        return path

    def skip(self, url: str, reason: str, *, session_id: str = "") -> None:
        """Record a capture that did not happen. Append only."""
        self.create()
        with self.skipped_path.open("a") as handle:
            handle.write(
                json.dumps({"url": url, "reason": reason, "session_id": session_id}) + "\n"
            )

    def history(self, url: str) -> list[tuple[str, str, str]]:
        """Every retrieval of a URL: when, and both digests. This is what an index buys."""
        with self.connect() as db:
            return db.execute(
                "SELECT fetched_at, bytes_sha256, text_sha256 FROM retrievals "
                "WHERE url = ? OR final_url = ? ORDER BY fetched_at",
                (url, url),
            ).fetchall()

    def drifted(self) -> list[tuple[str, int, int]]:
        """URLs fetched more than once whose text digest changed between fetches.

        Reported on the text digest, never the bytes: byte divergence on a live page is close to
        universal and says nothing about whether the page still makes the same claim.
        """
        with self.connect() as db:
            return db.execute(
                "SELECT url, COUNT(*) AS fetches, COUNT(DISTINCT text_sha256) AS readings "
                "FROM retrievals GROUP BY url HAVING readings > 1 ORDER BY readings DESC"
            ).fetchall()
