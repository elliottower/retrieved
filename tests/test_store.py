import httpx
import pytest
import yaml

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
