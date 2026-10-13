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
from retrieved.wall import recognise

CHECKS_NAME = "answer_checks.jsonl"

# One row a fetch. The key was the bytes' digest alone once, so a page fetched again unchanged
# replaced its own row and the earlier fetch left the index: `history` showed one capture where
# there had been two, and the time of the first was gone from everywhere but the record.
SCHEMA = """
CREATE TABLE IF NOT EXISTS retrievals (
    bytes_sha256 TEXT NOT NULL,
    url          TEXT NOT NULL,
    final_url    TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER NOT NULL,
    text_sha256  TEXT NOT NULL,
    session_id   TEXT,
    PRIMARY KEY (bytes_sha256, fetched_at)
);
CREATE INDEX IF NOT EXISTS retrievals_url ON retrievals(url);
CREATE INDEX IF NOT EXISTS retrievals_text ON retrievals(text_sha256);
"""

# An index made before the key included the time is rebuilt with every row it holds. Run a
# second time, by a second process that saw the old key in the same moment, it copies the new
# table into a new table and changes nothing.
_REKEY = """
BEGIN IMMEDIATE;
ALTER TABLE retrievals RENAME TO retrievals_by_bytes;
CREATE TABLE retrievals (
    bytes_sha256 TEXT NOT NULL,
    url          TEXT NOT NULL,
    final_url    TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER NOT NULL,
    text_sha256  TEXT NOT NULL,
    session_id   TEXT,
    PRIMARY KEY (bytes_sha256, fetched_at)
);
INSERT INTO retrievals SELECT * FROM retrievals_by_bytes;
DROP TABLE retrievals_by_bytes;
COMMIT;
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
            keyed_by = [row[1] for row in db.execute("PRAGMA table_info(retrievals)") if row[5]]
            if keyed_by == ["bytes_sha256"]:
                try:
                    db.executescript(_REKEY)
                except sqlite3.Error:
                    # Left as it was. The old index still answers every question but one, and
                    # a capture is worth more than the history of an earlier one.
                    db.rollback()
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
        agent_id: str = "",
        prompt: str = "",
        answer_check: AnswerCheck | None = None,
        keep_bytes: bool = True,
    ) -> pathlib.Path:
        """Store the bytes and the record, and index the row. Returns the record's path.

        With `keep_bytes` false the bytes are not written, and the record says so. Everything
        measured of them is still there: both digests, the length, the status, the check of the
        answer. That is enough to say later whether a page still reads the same, and not enough
        to read it again.

        A record is named by its bytes, so a later fetch that returns the same bytes names the
        same file. Where that file already holds the check of an earlier answer it is left as
        it is, and this answer's check is appended to `answer_checks.jsonl` beside when the
        bytes came back again. The index gains a row either way: it holds one a fetch, so
        `history` shows a page that came back unchanged, and when.
        """
        self.create()
        blob = self.store / retrieval.bytes_sha256
        if keep_bytes:
            blob.write_bytes(retrieval.body)
        path = self.retrievals / f"{retrieval.bytes_sha256}.yaml"
        earlier = self._checked_record(path)

        record = {
            "url": retrieval.url,
            "final_url": retrieval.final_url,
            "fetched_at": retrieval.fetched_at,
            "http_status": retrieval.http_status,
            "content_type": retrieval.content_type,
            "bytes": retrieval.bytes_len,
            "bytes_sha256": retrieval.bytes_sha256,
            # Whether the bytes were stored with this record, kept now or by an earlier fetch
            # that returned the same ones. False is a choice made at capture and not a loss.
            "bytes_kept": blob.is_file(),
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
        # Present only where the page said it was one. A status of 200 over a check on the
        # browser reads as a capture of the article to everything that looks at the status
        # alone, and this is the one place a capture made by hand can say otherwise.
        if wall := recognise(retrieval):
            record["wall"] = wall
        # Present only where there was an answer to check: a capture made by hand has none. So a
        # record without the block was never checked, which is a third thing beside "checked" and
        # "could not be checked", and every record written before the check existed says it too.
        if answer_check is not None:
            record["answer_check"] = answer_check.as_record()
        if earlier is None:
            path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))
        elif answer_check is not None:
            if earlier.get("bytes_kept") is False and blob.is_file():
                # The earlier fetch kept no bytes and this one did. That one line of the record
                # is brought up to date, and the earlier answer's check beside it is untouched.
                earlier["bytes_kept"] = True
                path.write_text(yaml.safe_dump(earlier, sort_keys=False, allow_unicode=True))
            self._append_check(
                retrieval,
                answer_check,
                copy_fetched_at=earlier.get("fetched_at"),
                same_bytes_fetched_at=retrieval.fetched_at,
                session_id=session_id,
                agent_id=agent_id,
            )

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

    def skip(self, url: str, reason: str, *, session_id: str = "", agent_id: str = "") -> None:
        """Record a capture that did not happen. Append only.

        `agent_id` is written where a subagent made the fetch, and left out where none did, so
        a line from the main agent reads as every line did before subagents were recorded.
        """
        self.create()
        line = {"url": url, "reason": reason, "session_id": session_id}
        if agent_id:
            line["agent_id"] = agent_id
        with self.skipped_path.open("a") as handle:
            handle.write(json.dumps(line) + "\n")

    @staticmethod
    def kept_no_bytes(path: pathlib.Path) -> bool:
        """Whether a record says its bytes were never stored. False where it cannot be read."""
        try:
            record = yaml.safe_load(path.read_text())
        except (OSError, ValueError, yaml.YAMLError):
            return False
        return isinstance(record, dict) and record.get("bytes_kept") is False

    @staticmethod
    def _checked_record(path: pathlib.Path) -> dict | None:
        """The record at a path where it already carries an answer's check, else None.

        None for a file that is absent, unreadable or has no check: there is nothing in any of
        those that writing over would lose.
        """
        try:
            record = yaml.safe_load(path.read_text())
        except (OSError, ValueError, yaml.YAMLError):
            return None
        return record if isinstance(record, dict) and "answer_check" in record else None

    def _append_check(
        self,
        copy: Retrieval,
        check: AnswerCheck,
        *,
        copy_fetched_at: object,
        not_fetched_because: str | None = None,
        same_bytes_fetched_at: str | None = None,
        session_id: str = "",
        agent_id: str = "",
    ) -> None:
        """One line of `answer_checks.jsonl`: an answer checked against a copy already held."""
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
                        "copy_fetched_at": copy_fetched_at,
                        # One of these two is set. Either no request was made, and this is why;
                        # or one was, and it returned the bytes the record already names.
                        "not_fetched_because": not_fetched_because,
                        "same_bytes_fetched_at": same_bytes_fetched_at,
                        "answer_check": check.as_record(),
                    }
                )
                + "\n"
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
                "SELECT bytes_sha256, fetched_at FROM retrievals WHERE url = ? OR final_url = ? "
                "ORDER BY fetched_at DESC",
                (url, url),
            ).fetchall()
        # The time is the index's, which is the last time a fetch returned these bytes. The
        # record keeps the first, and the two differ where a page was fetched twice unchanged.
        for digest, fetched_at in rows:
            try:
                record = yaml.safe_load((self.retrievals / f"{digest}.yaml").read_text())
                body = (self.store / digest).read_bytes()
                if hashlib.sha256(body).hexdigest() != digest:
                    continue
                return Retrieval(
                    url=record["url"],
                    final_url=record["final_url"],
                    fetched_at=fetched_at,
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
        self._append_check(
            copy,
            check,
            copy_fetched_at=copy.fetched_at,
            not_fetched_because=declined,
            session_id=session_id,
            agent_id=agent_id,
        )

    def _appended_checks(self) -> list[dict]:
        """The `answer_check` block of each line of `answer_checks.jsonl` that has one."""
        try:
            lines = self.checks_path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        blocks = []
        for line in lines:
            try:
                block = json.loads(line)["answer_check"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(block, dict):
                blocks.append(block)
        return blocks

    def stored_checks(self) -> tuple[int, int]:
        """How many answers were checked against a copy already held: ran, and did not run.

        Counted apart from `answer_checks`, which counts records. A damaged line is passed over.
        """
        statuses = [block.get("status") for block in self._appended_checks()]
        ran = sum(status in (FOUND, NOT_FOUND) for status in statuses)
        return ran, statuses.count(NOT_RUN)

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

    def survey(self) -> dict[str, int]:
        """What the records say of the answer check, in one pass over them.

        `ran`, `not_run` and `absent` count records, and `walls` counts the records of pages
        that said they were one, checked or not. `longer_than_page` counts answers recorded
        as `answer_without_page_text`, in records and in `answer_checks.jsonl` alike: a thing
        measured and kept, and not a flag. A record that cannot be read counts as absent: this
        is a survey, and one damaged file must not end it.
        """
        counts = {"ran": 0, "not_run": 0, "absent": 0, "longer_than_page": 0, "walls": 0}
        for path in self.retrievals.glob("*.yaml"):
            try:
                record = yaml.safe_load(path.read_text())
                counts["walls"] += bool(record.get("wall"))
                block = record.get("answer_check")
                status = block.get("status")
            except (OSError, ValueError, yaml.YAMLError, AttributeError):
                counts["absent"] += 1
                continue
            if status == NOT_RUN:
                counts["not_run"] += 1
            elif status in (FOUND, NOT_FOUND):
                counts["ran"] += 1
            else:
                counts["absent"] += 1
            counts["longer_than_page"] += block.get("answer_without_page_text") is True
        for block in self._appended_checks():
            counts["longer_than_page"] += block.get("answer_without_page_text") is True
        return counts

    def answer_checks(self) -> tuple[int, int, int]:
        """How many records had the answer check run, not run, and not recorded at all."""
        counts = self.survey()
        return counts["ran"], counts["not_run"], counts["absent"]

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
