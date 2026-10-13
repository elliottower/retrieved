import json

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


def test_answer_checks_are_counted_as_run_not_run_and_never_recorded(tmp_path):
    library = Library(tmp_path / ".retrieved").create()
    for n, said in enumerate([QUOTED_RIGHT, QUOTED_WRONG, "It keeps photographs.", None]):
        r = a_retrieval(ARTICLE + f"<!--{n}-->".encode())
        library.write(r, answer_check=check(said, r))
    library.write(a_retrieval(ARTICLE + b"<!--by hand-->"))
    old = library.write(a_retrieval(ARTICLE + b"<!--damaged-->"))
    old.write_text("")
    assert library.answer_checks() == (2, 2, 2)
