import json
import sqlite3
import subprocess
import sys

import pytest

HOOK = [sys.executable, "-m", "retrieved.hook", "--capture"]
DETACHING = [sys.executable, "-m", "retrieved.hook"]


def run(payload, env, text=None):
    return subprocess.run(
        HOOK,
        input=text if text is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    import os

    e = dict(os.environ)
    e["RETRIEVED_HOME"] = str(tmp_path / ".retrieved")
    e["PYTHONPATH"] = "src"
    return e


@pytest.mark.parametrize("text", ["", "not json", "[]", "null"])
def test_the_hook_never_breaks_the_session(text, env):
    """Constraint 1. A hook that raises interrupts someone's work, so it may only exit 0."""
    out = run(None, env, text=text)
    assert out.returncode == 0, out.stderr


def test_a_payload_with_no_url_says_nothing(env):
    out = run({"tool_name": "WebFetch", "tool_input": {}}, env)
    assert out.returncode == 0
    assert out.stdout == ""


def test_a_refused_url_is_recorded_and_never_fetched(env, tmp_path):
    """The metadata endpoint reaches the hook exactly the way a real one would."""
    out = run(
        {
            "tool_name": "WebFetch",
            "tool_input": {"url": "http://169.254.169.254/latest/meta-data/"},
            "session_id": "s1",
        },
        env,
    )
    assert out.returncode == 0
    assert out.stdout == ""
    skipped = (tmp_path / ".retrieved" / "skipped.jsonl").read_text()
    assert "metadata" in skipped
    assert not list((tmp_path / ".retrieved" / "retrievals").glob("*.yaml"))


def test_an_unreachable_host_is_recorded_rather_than_raised(env, tmp_path):
    out = run(
        {
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://this-host-does-not-resolve.invalid/x"},
        },
        env,
    )
    assert out.returncode == 0
    assert "fetch failed" in (tmp_path / ".retrieved" / "skipped.jsonl").read_text()


def test_the_hook_stays_silent_on_success_and_failure_alike(env):
    """Constraint 3. Anything printed here lands in the agent's context on every fetch."""
    for payload in (
        {"tool_input": {"url": "http://localhost/x"}},
        {"tool_input": {"url": "https://nowhere.invalid/y"}},
        {"tool_input": {}},
    ):
        assert run(payload, env).stdout == ""


def test_the_detaching_hook_returns_before_the_fetch_finishes(env):
    """A PostToolUse hook runs synchronously, so the session waits for whatever it does. The
    default path must hand the work off and return, or a slow page stalls someone's editing."""
    import time

    started = time.monotonic()
    out = subprocess.run(
        DETACHING,
        input=json.dumps({"tool_input": {"url": "https://example.com/"}}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    elapsed = time.monotonic() - started
    assert out.returncode == 0
    assert out.stdout == ""
    assert elapsed < 1.0, f"the hook blocked for {elapsed:.1f}s"


def test_a_refused_url_never_spends_a_capture_against_the_session_cap(env, tmp_path):
    """The denylist is asked before the rate limiter, and the order is not cosmetic. Asked the
    other way, a session working against private hosts spends its whole 100-capture budget on
    requests that were never going to be made, and then stops capturing the real pages."""
    for octet in range(5):
        run(
            {
                "tool_name": "WebFetch",
                "tool_input": {"url": f"http://10.0.0.{octet}/admin"},
                "session_id": "s1",
            },
            env,
        )

    db = sqlite3.connect(tmp_path / ".retrieved" / "index.db")
    try:
        exists = db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'session_state'"
        ).fetchone()
        spent = (
            db.execute("SELECT COALESCE(SUM(captures), 0) FROM session_state").fetchone()[0]
            if exists
            else 0
        )
    finally:
        db.close()
    assert spent == 0
