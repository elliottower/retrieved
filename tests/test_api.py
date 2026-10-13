import json

import httpx
import pytest
import yaml

from retrieved import api, capture, politeness
from retrieved.store import Library

FETCH = capture.fetch
PAGE = b"<html><body><p>The ferry runs twice a day in winter.</p></body></html>"


@pytest.fixture
def library(tmp_path, monkeypatch):
    def offline(url, *, timeout=30.0, client=None):
        def handler(request):
            body = b"<title>Checking your browser</title>" if "walled" in url else PAGE
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            return FETCH(url, timeout=timeout, client=c)

    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: None)
    monkeypatch.setattr(api, "fetch", offline)
    return Library(tmp_path / ".retrieved")


def skipped(library):
    return [json.loads(line) for line in library.skipped_path.read_text().splitlines()]


def test_one_call_fetches_stores_records_and_paces(library):
    captured = api.capture("https://example.com/ferry", library=library, prompt="when?")
    record = yaml.safe_load(captured.record.read_text())
    assert (library.store / captured.retrieval.bytes_sha256).read_bytes() == PAGE
    assert (record["url"], record["prompt"]) == ("https://example.com/ferry", "when?")
    assert captured.wall == ""
    assert len(library.history("https://example.com/ferry")) == 1
    # Paced as the command and the hook are: the host was asked a moment ago.
    with pytest.raises(api.Declined) as caught:
        api.capture("https://example.com/fares", library=library)
    assert "minimum interval" in caught.value.reason
    assert skipped(library) == [
        {"url": "https://example.com/fares", "reason": caught.value.reason, "session_id": ""}
    ]


def test_a_caller_that_waits_gets_the_page_and_one_that_will_not_pace_gets_it_at_once(
    library, monkeypatch
):
    slept = []
    monkeypatch.setattr(politeness.time, "sleep", slept.append)
    api.capture("https://example.com/ferry", library=library)
    api.capture("https://example.com/fares", library=library, wait=10)
    assert len(slept) == 1
    api.capture("https://example.com/piers", library=library, pace=False)
    assert len(slept) == 1
    assert not library.skipped_path.exists()


def test_a_url_that_must_never_be_requested_is_refused_recorded_and_spends_no_slot(library):
    with pytest.raises(api.Refused):
        api.capture("http://169.254.169.254/latest/meta-data/", library=library, pace=False)
    (line,) = skipped(library)
    assert line["url"] == "http://169.254.169.254/latest/meta-data/"
    with library.connect() as db:
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
        assert "host_state" not in tables or not db.execute("SELECT * FROM host_state").fetchall()


def test_a_wall_is_kept_and_said_to_be_one(library):
    captured = api.capture("https://example.com/walled", library=library)
    assert captured.retrieval.http_status == 200
    assert captured.wall.startswith("a check on the browser")
    assert yaml.safe_load(captured.record.read_text())["wall"] == captured.wall


def test_a_request_that_fails_is_recorded_and_raised_as_it_was(library, monkeypatch):
    def broken(url):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(api, "fetch", broken)
    with pytest.raises(httpx.ReadTimeout):
        api.capture("https://example.com/ferry", library=library)
    assert skipped(library)[0]["reason"] == "fetch failed: ReadTimeout"
