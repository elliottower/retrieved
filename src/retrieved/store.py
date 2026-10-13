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
        flags.jsonl                   answers that did not match the page they were about
        answer_checks.jsonl           answers checked against a copy already held

`skipped.jsonl` is not an afterthought. A capture refused by the denylist or a rate limit is a
thing that did not happen, and a store that records only successes reports the same silence for
"nothing to capture" and "capture declined".
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import pathlib
import sqlite3
from collections.abc import Iterator

import yaml

from retrieved.answer import FOUND, NOT_FOUND, NOT_RUN, AnswerCheck
from retrieved.capture import Retrieval
from retrieved.flags import FLAGS_NAME, library_root

CHECKS_NAME = "answer_checks.jsonl"

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
        self.flags_path = root / FLAGS_NAME
        self.checks_path = root / CHECKS_NAME

    @classmethod
    def resolve(cls, start: pathlib.Path | None = None) -> Library:
        # The walk itself lives in `flags`, the one module the report hook can afford to import.
        # Two copies of it would be two answers to which library a session belongs to.
        return cls(library_root(start))

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
        self,
        retrieval: Retrieval,
        *,
        session_id: str = "",
        prompt: str = "",
        answer_check: AnswerCheck | None = None,
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
        # Present only where there was an answer to check: a capture made by hand has none. So a
        # record without the block was never checked, which is a third thing beside "checked" and
        # "could not be checked", and every record written before the check existed says it too.
        if answer_check is not None:
            record["answer_check"] = answer_check.as_record()
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

    def latest(self, url: str) -> Retrieval | None:
        """The most recent stored retrieval of a URL, bytes and all, or None where none reads.

        Newest first, and the first whose record parses and whose bytes still hash to their
        name. A record whose bytes were removed on request, or altered, is passed over for the
        one before it: this is asked in order to check an answer against the bytes, and bytes
        that are not what the record says are no evidence of anything.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT bytes_sha256 FROM retrievals WHERE url = ? OR final_url = ? "
                "ORDER BY fetched_at DESC",
                (url, url),
            ).fetchall()
        for (digest,) in rows:
            try:
                record = yaml.safe_load((self.retrievals / f"{digest}.yaml").read_text())
                body = (self.store / digest).read_bytes()
                if hashlib.sha256(body).hexdigest() != digest:
                    continue
                return Retrieval(
                    url=record["url"],
                    final_url=record["final_url"],
                    fetched_at=record["fetched_at"],
                    http_status=record["http_status"],
                    content_type=record["content_type"],
                    bytes_len=len(body),
                    bytes_sha256=digest,
                    text_sha256=record["text_sha256"],
                    extractor=record["extractor"],
                    rendering_method=record["rendering_method"],
                    javascript_executed=record["javascript_executed"],
                    response_headers=record.get("response_headers") or {},
                    body=body,
                    truncated=bool(record.get("truncated")),
                )
            except (OSError, ValueError, yaml.YAMLError, KeyError, TypeError):
                continue
        return None

    def checked_stored(
        self,
        copy: Retrieval,
        check: AnswerCheck,
        *,
        declined: str,
        session_id: str = "",
        agent_id: str = "",
    ) -> None:
        """Record an answer checked against a copy already held, not a new fetch. Append only.

        Not written into the copy's own record. That record is the account of one fetch and
        carries the check of the answer that fetch was made for; this is a later answer, about
        a page that was not requested again, and writing it there would replace the earlier
        check or leave a record saying two things about one event. One line a check, so several
        answers checked against one copy are several lines and none is lost.
        """
        self.create()
        with self.checks_path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "url": copy.url,
                        # Which copy, and when it was fetched: what this check is a check of.
                        "bytes_sha256": copy.bytes_sha256,
                        "copy_fetched_at": copy.fetched_at,
                        "not_fetched_because": declined,
                        "answer_check": check.as_record(),
                    }
                )
                + "\n"
            )

    def stored_checks(self) -> tuple[int, int]:
        """How many answers were checked against a copy already held: ran, and did not run.

        Counted apart from `answer_checks`, which counts records. A damaged line is passed over.
        """
        try:
            lines = self.checks_path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            return 0, 0
        ran = not_run = 0
        for line in lines:
            try:
                status = json.loads(line)["answer_check"]["status"]
            except (ValueError, KeyError, TypeError):
                continue
            if status == NOT_RUN:
                not_run += 1
            elif status in (FOUND, NOT_FOUND):
                ran += 1
        return ran, not_run

    def flag(
        self,
        retrieval: Retrieval,
        check: AnswerCheck,
        *,
        session_id: str = "",
        agent_id: str = "",
        stored_copy: bool = False,
    ) -> None:
        """Record an answer that did not match the page it was about. Append only.

        One line carries everything the note to the agent is built from, so the hook that
        delivers it reads this file and nothing else. `stored_copy` says the retrieval is one
        the library already held, and the line then carries when it was fetched, which the
        note has to say.
        """
        self.create()
        with self.flags_path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "url": retrieval.url,
                        "bytes_sha256": retrieval.bytes_sha256,
                        "copy_fetched_at": retrieval.fetched_at if stored_copy else None,
                        "http_status": retrieval.http_status,
                        "not_found": check.as_record()["not_found"],
                        "answer_without_page_text": check.answer_without_page_text,
                        "answer_chars": check.answer_chars,
                        "page_text_chars": check.page_text_chars,
                    }
                )
                + "\n"
            )

    def answer_checks(self) -> tuple[int, int, int]:
        """How many records had the answer check run, not run, and not recorded at all.

        A record that cannot be read counts with the last: this is a survey, and one damaged
        file must not end it.
        """
        ran = not_run = absent = 0
        for path in self.retrievals.glob("*.yaml"):
            try:
                block = yaml.safe_load(path.read_text()).get("answer_check")
                status = block.get("status")
            except (OSError, ValueError, yaml.YAMLError, AttributeError):
                absent += 1
                continue
            if status == NOT_RUN:
                not_run += 1
            elif status in (FOUND, NOT_FOUND):
                ran += 1
            else:
                absent += 1
        return ran, not_run, absent

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
