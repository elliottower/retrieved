import json
import sqlite3

import httpx
import pytest
import yaml

from retrieved.answer import check
from retrieved.capture import fetch
from retrieved.store import Library


def a_retrieval(body=b"<html><body>the claim</body></html>", url="https://example.com/"):
    def handler(request):
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html"}, request=request
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        return fetch(url, client=c)


def test_a_retrieval_writes_bytes_a_record_and_an_index_row(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval()
    path = library.write(r, session_id="s1", prompt="what does it claim?")

    assert (library.store / r.bytes_sha256).read_bytes() == r.body
    record = yaml.safe_load(path.read_text())
    assert record["bytes_sha256"] == r.bytes_sha256
    assert record["text_sha256"] == r.text_sha256
    assert record["prompt"] == "what does it claim?"
    assert library.history("https://example.com/") == [
        (r.fetched_at, r.bytes_sha256, r.text_sha256)
    ]


def test_the_record_says_it_is_an_independent_retrieval(tmp_path):
    """The one thing this library must never let a reader assume."""
    library = Library(tmp_path / ".retrieved").create()
    record = yaml.safe_load(library.write(a_retrieval()).read_text())
    assert "not necessarily the bytes any agent saw" in record["note"]


def test_drift_is_reported_on_the_text_digest_not_the_bytes(tmp_path):
    """Two fetches whose markup differs but whose text does not are not drift."""
    library = Library(tmp_path / ".retrieved").create()
    library.write(a_retrieval(b"<html><body>same claim<!--1--></body></html>"))
    library.write(a_retrieval(b"<html><body>same claim<!--2--></body></html>"))
    assert library.drifted() == []

    library.write(a_retrieval(b"<html><body>a different claim</body></html>"))
    assert library.drifted() == [("https://example.com/", 3, 2)]


def test_a_skipped_capture_is_recorded_rather_than_silent(tmp_path):
    """A store holding only successes reports the same silence for 'nothing to capture' and
    'capture declined', which is the distinction this whole project exists to keep."""
    library = Library(tmp_path / ".retrieved").create()
    library.skip("http://169.254.169.254/", "cloud instance metadata endpoint", session_id="s1")
    lines = library.skipped_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert "metadata" in lines[0]


def test_resolve_walks_up_to_the_projects_library(tmp_path, monkeypatch):
    monkeypatch.delenv("RETRIEVED_HOME", raising=False)
    (tmp_path / ".retrieved").mkdir()
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert Library.resolve(nested).root == (tmp_path / ".retrieved").resolve()


def test_retrieved_home_wins_over_the_walk(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("RETRIEVED_HOME", str(elsewhere))
    (tmp_path / ".retrieved").mkdir()
    assert Library.resolve(tmp_path).root == elsewhere.resolve()


@pytest.mark.parametrize("header", ["set-cookie", "authorization"])
def test_a_credential_header_cannot_reach_a_written_record(tmp_path, header):
    library = Library(tmp_path / ".retrieved").create()

    def handler(request):
        return httpx.Response(
            200,
            content=b"x",
            headers={"content-type": "text/html", header: "leaked"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = fetch("https://example.com/", client=c)
    assert "leaked" not in library.write(r).read_text()


ARTICLE = (
    b"<html><body><p>The archive opened in 1958 in a converted grain store beside the canal, "
    b"and for its first decade it held nothing but the minutes of the parish council.</p>"
    b"<p>It now keeps some forty thousand photographs, most of them given by families who "
    b"had no room for them, and a reading room that seats nine.</p><p>Readers are asked to "
    b"wash their hands, to use pencil, and to leave the boxes in the order they were found."
    b"</p><p>Nothing may be borrowed, and nothing ever has been.</p></body></html>"
)
QUOTED_RIGHT = 'It "now keeps some forty thousand photographs," the page says.'
QUOTED_WRONG = 'It "now keeps some ninety thousand photographs," the page says.'


def test_a_record_gains_the_answer_check_it_was_written_with(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    outcome = check(QUOTED_WRONG, r)
    record = yaml.safe_load(library.write(r, answer_check=outcome).read_text())

    assert record["answer_check"] == outcome.as_record()
    assert record["answer_check"]["status"] == "not_found"
    assert record["answer_check"]["not_found"] == ["now keeps some ninety thousand photographs,"]
    assert QUOTED_WRONG not in yaml.safe_dump(record)
    # Everything a record said before still says it.
    assert record["bytes_sha256"] == r.bytes_sha256
    assert "not necessarily the bytes any agent saw" in record["note"]


def test_a_capture_with_no_answer_to_check_carries_no_block(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    record = yaml.safe_load(library.write(a_retrieval(ARTICLE)).read_text())
    assert "answer_check" not in record


def test_a_flag_is_one_appended_line_holding_what_the_note_needs(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    library.flag(r, check(QUOTED_WRONG, r), session_id="s1")
    library.flag(r, check(QUOTED_WRONG, r), session_id="s2")

    first, second = (json.loads(line) for line in library.flags_path.read_text().splitlines())
    assert (first["session_id"], second["session_id"]) == ("s1", "s2")
    assert first["url"] == "https://example.com/"
    assert first["bytes_sha256"] == r.bytes_sha256
    assert first["http_status"] == 200
    assert first["not_found"] == ["now keeps some ninety thousand photographs,"]
    assert first["answer_without_page_text"] is False
    assert first["answer_chars"] == len(QUOTED_WRONG)
    # A new fetch by the main agent: no agent, and no copy to give the time of.
    assert (first["agent_id"], first["copy_fetched_at"]) == ("", None)


def test_a_flag_records_the_agent_and_for_a_stored_copy_when_it_was_fetched(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    library.flag(r, check(QUOTED_WRONG, r), session_id="s1", agent_id="a1", stored_copy=True)
    (flag,) = (json.loads(line) for line in library.flags_path.read_text().splitlines())
    assert (flag["agent_id"], flag["copy_fetched_at"]) == ("a1", r.fetched_at)


def test_answer_checks_are_counted_as_run_not_run_and_never_recorded(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    for n, said in enumerate([QUOTED_RIGHT, QUOTED_WRONG, "It keeps photographs.", None]):
        r = a_retrieval(ARTICLE + f"<!--{n}-->".encode())
        library.write(r, answer_check=check(said, r))
    library.write(a_retrieval(ARTICLE + b"<!--by hand-->"))
    old = library.write(a_retrieval(ARTICLE + b"<!--damaged-->"))
    old.write_text("")
    assert library.answer_checks() == (2, 2, 2)


def test_the_latest_copy_of_a_url_comes_back_as_the_retrieval_it_was(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    assert library.latest("https://example.com/") is None

    older = a_retrieval(ARTICLE + b"<!--older-->")
    older.fetched_at = "2026-01-01T00:00:00+00:00"
    newer = a_retrieval(ARTICLE + b"<!--newer-->")
    newer.fetched_at = "2026-02-01T00:00:00+00:00"
    library.write(newer)
    library.write(older)
    library.write(a_retrieval(ARTICLE + b"<!--elsewhere-->", url="https://example.org/"))

    copy = library.latest("https://example.com/")
    assert (copy.bytes_sha256, copy.fetched_at) == (newer.bytes_sha256, newer.fetched_at)
    assert copy.body == newer.body
    assert (copy.content_type, copy.http_status, copy.truncated) == ("text/html", 200, False)
    # What the check reads off it, it reads the same as off the original.
    assert check(QUOTED_WRONG, copy) == check(QUOTED_WRONG, newer)


def test_a_copy_whose_bytes_are_gone_or_altered_is_passed_over_for_the_one_before(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    older = a_retrieval(ARTICLE + b"<!--older-->")
    older.fetched_at = "2026-01-01T00:00:00+00:00"
    newer = a_retrieval(ARTICLE + b"<!--newer-->")
    newer.fetched_at = "2026-02-01T00:00:00+00:00"
    library.write(older)
    library.write(newer)

    (library.store / newer.bytes_sha256).write_bytes(b"not what the record says")
    assert library.latest("https://example.com/").bytes_sha256 == older.bytes_sha256

    (library.store / newer.bytes_sha256).unlink()
    assert library.latest("https://example.com/").bytes_sha256 == older.bytes_sha256

    (library.retrievals / f"{older.bytes_sha256}.yaml").write_text("")
    assert library.latest("https://example.com/") is None


def test_a_check_against_a_stored_copy_is_appended_and_leaves_the_copys_record_alone(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    path = library.write(r, session_id="s1", answer_check=check(QUOTED_RIGHT, r))
    before = path.read_text()

    library.checked_stored(
        r, check(QUOTED_WRONG, r), declined="captured 12s ago", session_id="s2", agent_id="a1"
    )
    library.checked_stored(r, check(QUOTED_RIGHT, r), declined="captured 40s ago")
    assert path.read_text() == before

    first, second = (json.loads(line) for line in library.checks_path.read_text().splitlines())
    assert (first["session_id"], first["agent_id"]) == ("s2", "a1")
    assert (first["url"], first["bytes_sha256"]) == (r.url, r.bytes_sha256)
    assert first["copy_fetched_at"] == r.fetched_at
    assert first["not_fetched_because"] == "captured 12s ago"
    assert first["same_bytes_fetched_at"] is None
    assert first["answer_check"] == check(QUOTED_WRONG, r).as_record()
    assert QUOTED_WRONG not in json.dumps(first)
    assert second["answer_check"]["status"] == "found"


def test_checks_against_stored_copies_are_counted_apart_from_records(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    assert library.stored_checks() == (0, 0)
    r = a_retrieval(ARTICLE)
    library.write(r, answer_check=check(QUOTED_RIGHT, r))
    for said in (QUOTED_RIGHT, QUOTED_WRONG, "It keeps photographs.", None):
        library.checked_stored(r, check(said, r), declined="captured 12s ago")
    with library.checks_path.open("a") as handle:
        handle.write("not json\n[]\n{}\n")
    assert library.stored_checks() == (2, 2)
    assert library.answer_checks() == (1, 0, 0)


def test_a_later_fetch_of_the_same_bytes_does_not_replace_the_earlier_answers_check(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    r.fetched_at = "2026-01-01T00:00:00+00:00"
    path = library.write(r, session_id="s1", prompt="first", answer_check=check(QUOTED_RIGHT, r))
    before = path.read_text()

    again = a_retrieval(ARTICLE)
    again.fetched_at = "2026-03-01T00:00:00+00:00"
    assert again.bytes_sha256 == r.bytes_sha256
    wrong = check(QUOTED_WRONG, again)
    assert library.write(again, session_id="s2", agent_id="a1", answer_check=wrong) == path

    assert path.read_text() == before
    assert yaml.safe_load(before)["answer_check"]["status"] == "found"
    (line,) = (json.loads(line) for line in library.checks_path.read_text().splitlines())
    assert (line["session_id"], line["agent_id"]) == ("s2", "a1")
    assert (line["url"], line["bytes_sha256"]) == (r.url, r.bytes_sha256)
    assert line["copy_fetched_at"] == "2026-01-01T00:00:00+00:00"
    assert line["same_bytes_fetched_at"] == "2026-03-01T00:00:00+00:00"
    assert line["not_fetched_because"] is None
    assert line["answer_check"] == wrong.as_record()

    # The index holds both fetches, and the copy handed out is the later.
    assert library.history(r.url) == [
        ("2026-01-01T00:00:00+00:00", r.bytes_sha256, r.text_sha256),
        ("2026-03-01T00:00:00+00:00", r.bytes_sha256, r.text_sha256),
    ]
    assert library.latest(r.url).fetched_at == again.fetched_at
    assert library.answer_checks() == (1, 0, 0)
    assert library.stored_checks() == (1, 0)


def test_a_capture_by_hand_of_the_same_bytes_leaves_a_checked_record_alone(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    path = library.write(r, answer_check=check(QUOTED_WRONG, r))
    before = path.read_text()
    library.write(a_retrieval(ARTICLE), prompt="by hand")
    assert path.read_text() == before
    assert not library.checks_path.exists()


def test_a_record_with_no_check_to_lose_is_written_again_as_it_always_was(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    path = library.write(a_retrieval(ARTICLE), prompt="by hand")
    r = a_retrieval(ARTICLE)
    library.write(r, prompt="by the hook", answer_check=check(QUOTED_WRONG, r))
    record = yaml.safe_load(path.read_text())
    assert (record["prompt"], record["answer_check"]["status"]) == ("by the hook", "not_found")
    assert not library.checks_path.exists()


def test_answers_longer_than_their_pages_text_are_counted_wherever_they_were_recorded(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    wall = b"<html><body><p>Please sign in to continue reading.</p></body></html>"
    long = "The archive keeps some forty thousand photographs. " * 20
    for n in range(2):
        r = a_retrieval(wall + f"<!--{n}-->".encode())
        outcome = check(long, r)
        assert outcome.answer_without_page_text
        library.write(r, answer_check=outcome)
    library.checked_stored(r, check(long, r), declined="captured 12s ago")
    article = a_retrieval(ARTICLE)
    library.write(article, answer_check=check(long, article))
    assert library.survey() == {
        "ran": 0,
        "not_run": 3,
        "absent": 0,
        "longer_than_page": 3,
        "walls": 2,
    }
    assert not library.flags_path.exists()


OLD_SCHEMA = """
CREATE TABLE retrievals (
    bytes_sha256 TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    final_url    TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER NOT NULL,
    text_sha256  TEXT NOT NULL,
    session_id   TEXT
);
CREATE INDEX retrievals_url ON retrievals(url);
"""


def test_an_index_keyed_by_bytes_alone_is_rebuilt_with_every_row_it_held(tmp_path):
    root = tmp_path / ".retrieved"
    root.mkdir()
    db = sqlite3.connect(root / "index.db")
    db.executescript(OLD_SCHEMA)
    rows = [
        (
            f"{n:064x}",
            "https://example.com/",
            "https://example.com/",
            f"2026-01-0{n}",
            200,
            "t",
            "s",
        )
        for n in range(1, 4)
    ]
    db.executemany("INSERT INTO retrievals VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()

    library = Library(root).create()
    with library.connect() as db:
        assert db.execute("SELECT * FROM retrievals ORDER BY fetched_at").fetchall() == rows
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
    assert "retrievals_by_bytes" not in tables
    assert {"retrievals_url", "retrievals_text"} <= tables

    # The reason for the new key: the same bytes at a later time are a second row.
    r = a_retrieval()
    for when in ("2026-02-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00"):
        r.fetched_at = when
        library.write(r)
    assert [row[0] for row in library.history(r.url)][-2:] == [
        "2026-02-01T00:00:00+00:00",
        "2026-02-02T00:00:00+00:00",
    ]
    # And creating the library again, as every write does, moves nothing.
    Library(root).create()
    assert len(library.history(r.url)) == 5


@pytest.mark.parametrize(
    "key", ["bytes_sha256 TEXT PRIMARY KEY", "bytes_sha256 TEXT NOT NULL"], ids=["v1", "v2"]
)
def test_an_index_under_either_earlier_key_ends_keyed_by_bytes_url_and_time(tmp_path, key):
    root = tmp_path / ".retrieved"
    root.mkdir()
    db = sqlite3.connect(root / "index.db")
    tail = "" if "PRIMARY" in key else ", PRIMARY KEY (bytes_sha256, fetched_at)"
    db.executescript(
        f"CREATE TABLE retrievals ({key}, url TEXT NOT NULL, final_url TEXT NOT NULL, "
        f"fetched_at TEXT NOT NULL, http_status INTEGER NOT NULL, text_sha256 TEXT NOT NULL, "
        f"session_id TEXT{tail});"
    )
    row = ("d" * 64, "https://example.com/a", "https://example.com/a", "2026-01-01", 200, "t", "s")
    db.execute("INSERT INTO retrievals VALUES (?,?,?,?,?,?,?)", row)
    db.commit()
    db.close()

    library = Library(root).create()
    with library.connect() as db:
        keyed_by = [r[1] for r in db.execute("PRAGMA table_info(retrievals)") if r[5]]
        assert keyed_by == ["bytes_sha256", "url", "fetched_at"]
        assert db.execute("SELECT * FROM retrievals").fetchall() == [row]

    # Two addresses, the same bytes, the same second: two rows.
    for url in ("https://example.com/x", "https://example.com/y"):
        r = a_retrieval(b"", url=url)
        r.fetched_at = "2026-02-01T00:00:00+00:00"
        library.write(r)
    assert len(library.history("https://example.com/x")) == 1
    assert len(library.history("https://example.com/y")) == 1


def test_a_url_fetched_twice_unchanged_is_two_fetches_and_one_reading(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval()
    for when in ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"):
        r.fetched_at = when
        library.write(r)
    assert len(library.history(r.url)) == 2
    assert library.drifted() == []
    assert len(list(library.retrievals.glob("*.yaml"))) == 1


def test_a_record_written_without_its_bytes_says_so_and_keeps_what_was_measured(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval()
    record = yaml.safe_load(library.write(r, keep_bytes=False).read_text())
    assert not (library.store / r.bytes_sha256).exists()
    assert record["bytes_kept"] is False
    assert (record["bytes"], record["bytes_sha256"], record["text_sha256"]) == (
        r.bytes_len,
        r.bytes_sha256,
        r.text_sha256,
    )
    assert library.history(r.url) == [(r.fetched_at, r.bytes_sha256, r.text_sha256)]
    # There are no bytes to check an answer against, so no copy is handed out.
    assert library.latest(r.url) is None
    assert yaml.safe_load(library.write(a_retrieval(ARTICLE)).read_text())["bytes_kept"] is True


def test_bytes_kept_by_a_later_fetch_are_noted_without_touching_the_earlier_check(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    r = a_retrieval(ARTICLE)
    first = check(QUOTED_WRONG, r)
    path = library.write(r, answer_check=first, keep_bytes=False)
    again = a_retrieval(ARTICLE)
    again.fetched_at = "2026-03-01T00:00:00+00:00"
    library.write(again, answer_check=check(QUOTED_WRONG, again), keep_bytes=True)
    record = yaml.safe_load(path.read_text())
    assert record["bytes_kept"] is True
    assert record["fetched_at"] == r.fetched_at
    assert record["answer_check"] == first.as_record()
    assert library.latest(r.url).body == ARTICLE


def test_a_skip_names_the_subagent_only_where_one_made_the_fetch(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    library.skip("https://example.com/a", "captured 3s ago", session_id="s1")
    library.skip("https://example.com/b", "captured 4s ago", session_id="s1", agent_id="a9")
    first, second = (json.loads(line) for line in library.skipped_path.read_text().splitlines())
    assert first == {
        "url": "https://example.com/a",
        "reason": "captured 3s ago",
        "session_id": "s1",
    }
    assert second["agent_id"] == "a9"
