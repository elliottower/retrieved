import io
import json
import sqlite3
import subprocess
import sys

import httpx
import pytest
import yaml

from retrieved import capture, flags, hook, politeness
from retrieved.store import Library

HOOK = [sys.executable, "-m", "retrieved.hook", "--capture"]
DETACHING = [sys.executable, "-m", "retrieved.hook"]
REPORT = [sys.executable, "-m", "retrieved.hook", "--report"]

FETCH = capture.fetch

# Written for these tests, and long enough to be a page that came with its content.
PAGE = (
    b"<html><body><p>The ferry to Skerry Holm sails twice a day from the old quay, weather "
    b"permitting, and takes forty minutes in a calm sea.</p><p>Tickets are sold on board and "
    b"cannot be reserved, a rule the company has kept since it began running the route in "
    b"1934.</p><p>Dogs travel free. Bicycles are carried on deck at the owner's risk, and in "
    b"winter the afternoon sailing is cancelled whenever the light fails before four.</p>"
    b"<p>The crossing has been missed only eleven times in the past decade.</p></body></html>"
)
WALL = b"<html><body><h1>Sign in</h1><p>Please sign in to continue.</p></body></html>"

ON_THE_PAGE = 'The page says "tickets are sold on board and cannot be reserved." It goes on.'
NOT_ON_THE_PAGE = 'The page says "tickets are sold online and must be reserved a week ahead".'


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


def capture_in_process(monkeypatch, payload, body=PAGE, ctype="text/html", status=200):
    """The detached worker, run here rather than as a subprocess so the page it fetches can be
    answered from memory. The hook imports `fetch` when it runs, so replacing it on the module
    is what the worker sees."""

    def offline(url, *, timeout=30.0, client=None):
        def handler(request):
            return httpx.Response(status, content=body, headers={"content-type": ctype})

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            return FETCH(url, timeout=timeout, client=c)

    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: None)
    monkeypatch.setattr(capture, "fetch", offline)
    monkeypatch.setattr(sys, "argv", ["retrieved-hook", "--capture"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hook.main()


def fetched(url, result, session="s1"):
    return {
        "tool_name": "WebFetch",
        "tool_input": {"url": url, "prompt": "how are tickets sold?"},
        "tool_response": {"bytes": len(PAGE), "code": 200, "result": result},
        "session_id": session,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIEVED_HOME", str(tmp_path / ".retrieved"))
    return tmp_path / ".retrieved"


def the_record(home):
    (path,) = (home / "retrievals").glob("*.yaml")
    return yaml.safe_load(path.read_text())


def report(env, payload=None, text=None):
    return subprocess.run(
        REPORT,
        input=text if text is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_an_answer_whose_quotation_is_on_the_page_is_recorded_and_raises_nothing(
    home, monkeypatch, capsys
):
    assert capture_in_process(monkeypatch, fetched("https://example.com/ferry", ON_THE_PAGE)) == 0
    assert capsys.readouterr() == ("", "")
    block = the_record(home)["answer_check"]
    assert (block["status"], block["quotations"], block["found"]) == ("found", 1, 1)
    assert block["not_found"] == []
    assert not (home / "flags.jsonl").exists()


def test_a_quotation_missing_from_the_page_writes_exactly_one_flag(home, monkeypatch, capsys):
    payload = fetched("https://example.com/ferry", NOT_ON_THE_PAGE, session="s7")
    assert capture_in_process(monkeypatch, payload) == 0
    assert capsys.readouterr() == ("", "")

    record = the_record(home)
    passage = "tickets are sold online and must be reserved a week ahead"
    assert record["answer_check"]["status"] == "not_found"
    assert record["answer_check"]["not_found"] == [passage]
    assert NOT_ON_THE_PAGE not in yaml.safe_dump(record)

    (line,) = (home / "flags.jsonl").read_text().splitlines()
    flag = json.loads(line)
    assert flag["session_id"] == "s7"
    assert flag["url"] == "https://example.com/ferry"
    assert flag["bytes_sha256"] == record["bytes_sha256"]
    assert flag["not_found"] == [passage]
    assert flag["answer_without_page_text"] is False
    assert flag["at"]


def test_a_long_answer_over_a_login_wall_writes_the_other_flag(home, monkeypatch, capsys):
    said = "The ferry sails twice a day and takes forty minutes. " * 20
    payload = fetched("https://example.com/ferry", said)
    assert capture_in_process(monkeypatch, payload, body=WALL, status=403) == 0
    assert capsys.readouterr() == ("", "")

    block = the_record(home)["answer_check"]
    assert block["status"] == "not_run"
    assert block["answer_without_page_text"] is True
    (line,) = (home / "flags.jsonl").read_text().splitlines()
    flag = json.loads(line)
    assert flag["answer_without_page_text"] is True
    assert flag["not_found"] == []
    assert (flag["answer_chars"], flag["http_status"]) == (len(said), 403)
    assert flag["page_text_chars"] == len("Sign in Please sign in to continue.")


def test_a_pdf_nothing_can_read_is_recorded_as_not_checked_and_raises_nothing(
    home, monkeypatch, capsys
):
    payload = fetched("https://example.com/timetable.pdf", NOT_ON_THE_PAGE)
    body = b"%PDF-1.7 " + bytes(range(256)) * 8
    assert capture_in_process(monkeypatch, payload, body=body, ctype="application/pdf") == 0
    assert capsys.readouterr() == ("", "")
    block = the_record(home)["answer_check"]
    assert block["status"] == "not_run"
    # No extractor installed, or one that could not read these bytes: either way a reason
    # that names the extractor, and never a passage reported missing.
    assert "pdftotext" in block["reason"]
    assert block["not_found"] == []
    assert not (home / "flags.jsonl").exists()


@pytest.mark.parametrize(
    "tool_response",
    ["a bare string", None, ["a", "list"], 7, {}, {"result": None}, {"result": {"nested": 1}}],
)
def test_a_tool_response_with_no_answer_still_captures_and_says_the_check_did_not_run(
    tool_response, home, monkeypatch, capsys
):
    payload = fetched("https://example.com/ferry", "")
    payload["tool_response"] = tool_response
    assert capture_in_process(monkeypatch, payload) == 0
    assert capsys.readouterr() == ("", "")
    block = the_record(home)["answer_check"]
    assert (block["status"], block["reason"]) == ("not_run", "no answer in the payload")
    assert not (home / "flags.jsonl").exists()


def test_a_payload_with_no_tool_response_at_all_still_captures(home, monkeypatch, capsys):
    payload = {"tool_input": {"url": "https://example.com/ferry"}}
    assert capture_in_process(monkeypatch, payload) == 0
    assert capsys.readouterr() == ("", "")
    assert the_record(home)["answer_check"]["status"] == "not_run"


def test_a_check_that_raises_costs_neither_the_capture_nor_the_silence(home, monkeypatch, capsys):
    def broken(answer, retrieval):
        raise RuntimeError("a defect in the check")

    monkeypatch.setattr("retrieved.answer.check", broken)
    assert capture_in_process(monkeypatch, fetched("https://example.com/ferry", ON_THE_PAGE)) == 0
    assert capsys.readouterr() == ("", "")
    block = the_record(home)["answer_check"]
    assert (block["status"], block["reason"]) == ("not_run", "check failed: RuntimeError")


def test_the_report_tells_the_right_session_once_and_nobody_else(home, monkeypatch, env):
    payload = fetched("https://example.com/ferry", NOT_ON_THE_PAGE, session="s7")
    capture_in_process(monkeypatch, payload)

    other = report(env, {"session_id": "s8", "hook_event_name": "PostToolUse"})
    assert (other.returncode, other.stdout, other.stderr) == (0, "", "")

    first = report(env, {"session_id": "s7", "hook_event_name": "PostToolUse"})
    assert first.returncode == 0
    said = json.loads(first.stdout)
    assert set(said) == {"hookSpecificOutput"}
    assert set(said["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
    assert said["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert said["hookSpecificOutput"]["additionalContext"] == (
        "retrieved: 1 passage the fetch answer quoted from https://example.com/ferry is not in "
        'the page as retrieved: "tickets are sold online and must be reserved a week ahead". '
        "Read the page before quoting it. The page was fetched again, independently, and a "
        "second fetch can differ from what the fetch tool read."
    )

    second = report(env, {"session_id": "s7", "hook_event_name": "PostToolUse"})
    assert (second.returncode, second.stdout, second.stderr) == (0, "", "")


def test_the_report_says_nothing_when_nothing_was_flagged(home, monkeypatch, env):
    capture_in_process(monkeypatch, fetched("https://example.com/ferry", ON_THE_PAGE))
    out = report(env, {"session_id": "s1", "hook_event_name": "PostToolUse"})
    assert (out.returncode, out.stdout, out.stderr) == (0, "", "")


def test_the_report_says_nothing_where_there_is_no_library(env, tmp_path):
    out = report(env, {"session_id": "s1"})
    assert (out.returncode, out.stdout, out.stderr) == (0, "", "")
    assert not (tmp_path / ".retrieved").exists()


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", "null", '{"session_id": 5}', '{"session_id": ""}', "{}"]
)
def test_malformed_input_to_the_report_exits_zero_in_silence(text, home, monkeypatch, env):
    capture_in_process(monkeypatch, fetched("https://example.com/ferry", NOT_ON_THE_PAGE, ""))
    out = report(env, text=text)
    assert (out.returncode, out.stdout, out.stderr) == (0, "", "")


def test_a_damaged_line_does_not_hide_the_flag_after_it(home, monkeypatch, env):
    home.mkdir()
    (home / "flags.jsonl").write_text('not json\n["a list"]\n')
    capture_in_process(monkeypatch, fetched("https://example.com/ferry", NOT_ON_THE_PAGE))
    out = report(env, {"session_id": "s1"})
    assert (
        "tickets are sold online"
        in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    )


def test_the_note_for_an_answer_without_page_text_gives_both_sizes_and_the_status(
    home, monkeypatch, env
):
    said = "The ferry sails twice a day and takes forty minutes. " * 20
    capture_in_process(
        monkeypatch, fetched("https://example.com/ferry", said), body=WALL, status=403
    )
    out = report(env, {"session_id": "s1"})
    assert json.loads(out.stdout)["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            f"retrieved: the fetch answer for https://example.com/ferry runs to {len(said)} "
            "characters, and the page as retrieved holds 35 characters of text (HTTP 403). The "
            "answer may not come from the page. The page was fetched again, independently, and "
            "a second fetch can differ from what the fetch tool read."
        ),
    }


def test_several_passages_are_counted_and_the_note_names_no_more_than_it_says(home):
    passages = [f"passage number {n} that the page never contained" for n in range(7)]
    text = flags.note({"url": "https://example.com/x", "not_found": passages})
    assert text.startswith("retrieved: 7 passages the fetch answer quoted from")
    assert " are not in the page as retrieved: " in text
    assert text.count("that the page never contained") == flags.NOTE_PASSAGES
    assert f"and {7 - flags.NOTE_PASSAGES} more." in text
    assert flags.INDEPENDENT in text


def test_parallel_reports_deliver_a_flag_exactly_once(home, monkeypatch, env):
    capture_in_process(monkeypatch, fetched("https://example.com/ferry", NOT_ON_THE_PAGE))
    running = [
        subprocess.Popen(
            REPORT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        for _ in range(12)
    ]
    for process in running:
        process.stdin.write(json.dumps({"session_id": "s1"}).encode())
        process.stdin.close()
    spoken = []
    for process in running:
        assert process.wait(timeout=30) == 0
        spoken.append(process.stdout.read())
        assert process.stderr.read() == b""
        process.stdout.close()
        process.stderr.close()
    assert len([out for out in spoken if out]) == 1
    assert len(list((home / "flags.delivered").iterdir())) == 1


def test_only_the_first_claim_on_a_flag_succeeds(tmp_path):
    # What the parallel test above relies on, without the timing: a second process that got
    # past the "already delivered?" look in the same instant still loses here.
    assert flags._claim(tmp_path, "0123abcd") is True
    assert flags._claim(tmp_path, "0123abcd") is False
    assert flags._claim(tmp_path, "4567ef01") is True


def test_a_flag_whose_delivery_cannot_be_recorded_is_not_delivered(home, monkeypatch):
    capture_in_process(monkeypatch, fetched("https://example.com/ferry", NOT_ON_THE_PAGE))
    (home / "flags.delivered").write_text("a file where the directory should be")
    assert flags.undelivered(home, "s1") == []


def test_the_hook_imports_nothing_outside_the_standard_library_until_it_captures(env):
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, retrieved.hook; "
            "print(sorted(m for m in ('httpx', 'yaml', 'pydantic') if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert loaded.stdout.strip() == "[]", loaded.stderr


def test_the_report_resolves_the_library_a_capture_from_the_same_directory_writes_to(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("RETRIEVED_HOME", raising=False)
    (tmp_path / ".retrieved").mkdir()
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert flags.library_root(nested) == Library.resolve(nested).root
    assert flags.library_root(nested) == (tmp_path / ".retrieved").resolve()
