import datetime
import sqlite3

import pytest

from retrieved import politeness
from retrieved.politeness import Declined, check, record

AGENT = "retrieved/0.1"


@pytest.fixture
def db(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE retrievals (bytes_sha256 TEXT PRIMARY KEY, url TEXT, final_url TEXT, "
        "fetched_at TEXT, http_status INTEGER, text_sha256 TEXT, session_id TEXT);"
    )
    # No network in these tests: robots.txt is whatever the test says it is.
    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: None)
    yield connection
    connection.close()


def test_a_second_request_to_one_host_too_soon_is_declined(db):
    record(db, "https://example.com/a")
    with pytest.raises(Declined) as caught:
        check(db, "https://example.com/b", AGENT)
    assert "minimum interval" in caught.value.reason


def test_a_different_host_is_not_paced_by_the_first(db):
    record(db, "https://example.com/a")
    check(db, "https://other.example.org/b", AGENT)


def test_a_url_captured_moments_ago_is_not_captured_again(db):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    db.execute(
        "INSERT INTO retrievals VALUES ('d','https://example.com/x','https://example.com/x',?,200,'t','s')",
        (now,),
    )
    with pytest.raises(Declined) as caught:
        check(db, "https://example.com/x", AGENT)
    assert "ago" in caught.value.reason


def test_a_url_captured_long_ago_is_captured_again(db):
    old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat()
    db.execute(
        "INSERT INTO retrievals VALUES ('d','https://example.com/x','https://example.com/x',?,200,'t','s')",
        (old,),
    )
    check(db, "https://example.com/x", AGENT)


def test_the_session_cap_stops_a_runaway_session(db, monkeypatch):
    monkeypatch.setattr(politeness, "SESSION_CAP", 3)
    for i in range(3):
        record(db, f"https://h{i}.example.com/", session_id="s1")
    with pytest.raises(Declined) as caught:
        check(db, "https://fresh.example.com/", AGENT, session_id="s1")
    assert "session cap" in caught.value.reason


def test_one_session_hitting_the_cap_does_not_stop_another(db, monkeypatch):
    monkeypatch.setattr(politeness, "SESSION_CAP", 2)
    record(db, "https://a.example.com/", session_id="s1")
    record(db, "https://b.example.com/", session_id="s1")
    check(db, "https://c.example.com/", AGENT, session_id="s2")


def test_a_disallowed_path_is_declined(db, monkeypatch):
    import urllib.robotparser

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private/"])
    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: parser)

    with pytest.raises(Declined) as caught:
        check(db, "https://example.com/private/x", AGENT)
    assert "robots.txt" in caught.value.reason
    check(db, "https://example.com/public/x", AGENT)


def test_a_published_crawl_delay_widens_the_interval_but_never_narrows_it(db, monkeypatch):
    """A host asking for 60s gets 60s. A host asking for 0.1s still gets the floor."""
    import urllib.robotparser

    slow = urllib.robotparser.RobotFileParser()
    slow.parse(["User-agent: *", "Crawl-delay: 60"])
    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: slow)
    record(db, "https://example.com/a")
    with pytest.raises(Declined) as caught:
        check(db, "https://example.com/b", AGENT)
    assert "minimum interval 60s" in caught.value.reason

    eager = urllib.robotparser.RobotFileParser()
    eager.parse(["User-agent: *", "Crawl-delay: 0.1"])
    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: eager)
    with pytest.raises(Declined) as caught:
        check(db, "https://example.com/c", AGENT)
    assert f"minimum interval {politeness.MIN_HOST_INTERVAL:.0f}s" in caught.value.reason


def test_a_host_with_no_robots_is_still_paced(db):
    """Silence permits fetching. It does not permit fetching as fast as the server can answer."""
    record(db, "https://example.com/a")
    with pytest.raises(Declined):
        check(db, "https://example.com/b", AGENT)


def test_the_robots_fetch_goes_through_the_denylist(monkeypatch):
    """The rate limiter makes its own request, to a URL built from a host an agent named. It
    reached private addresses before any caller's denylist check did, so it checks its own."""

    def forbidden(url, **kwargs):
        raise AssertionError(f"the rate limiter requested {url}")

    monkeypatch.setattr(politeness.httpx, "get", forbidden)

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(politeness.SCHEMA)
        assert politeness._robots_for(connection, "10.0.0.1", "http") is None
        assert politeness._robots_for(connection, "169.254.169.254", "http") is None
        assert politeness._robots_for(connection, "localhost", "http") is None
    finally:
        connection.close()
