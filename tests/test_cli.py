"""The commands, exercised the way someone runs them.

The CLI had no tests. Every module beneath it did, which is the shape that hides a broken
command: the logic is right and the thing a person types is wrong. One of these found exactly
that -- `promotable` printed sixteen characters of a digest and `promote` demanded sixty-four.
"""

import json
import sqlite3

import httpx
import pytest

from retrieved import answer, capture, cli, politeness
from retrieved.store import Library


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIEVED_HOME", str(tmp_path / ".retrieved"))
    return tmp_path / ".retrieved"


def serve(monkeypatch, body=None, ctype="text/html"):
    """Make `fetch` answer from memory, so no command under test touches the network.

    The body varies with the URL unless one is given. Two captures of identical bytes share a
    digest and become one record, which silently turned a two-capture test into a one-capture
    test.

    robots.txt goes with it. The rate limiter reads one over the network, and a suite that
    reaches a real host is a suite that fails when that host is down.
    """
    monkeypatch.setattr(politeness, "_robots_for", lambda *a, **k: None)
    real = capture.fetch

    def offline(url, *, timeout=30.0, client=None):
        def handler(request):
            payload = body if body is not None else f"<html><body>{url}</body></html>".encode()
            return httpx.Response(
                200, content=payload, headers={"content-type": ctype}, request=request
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            return real(url, timeout=timeout, client=c)

    monkeypatch.setattr(cli, "fetch", offline)


def test_capture_writes_a_record_and_reports_both_digests(home, monkeypatch, capsys):
    serve(monkeypatch)
    assert cli.main(["capture", "https://arxiv.org/abs/2211.00593"]) == 0
    out = capsys.readouterr().out
    assert "captured" in out
    assert "bytes" in out and "text" in out
    assert len(list((home / "retrievals").glob("*.yaml"))) == 1


def test_capture_of_a_refused_url_exits_nonzero_and_records_why(home, capsys):
    assert cli.main(["capture", "http://169.254.169.254/latest/meta-data/"]) == 1
    assert "metadata" in capsys.readouterr().out
    assert "metadata" in (home / "skipped.jsonl").read_text()


def test_verify_reports_intact_bytes(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    capsys.readouterr()
    assert cli.main(["verify"]) == 0
    assert "1 intact" in capsys.readouterr().out


def test_verify_fails_when_stored_bytes_were_altered(home, monkeypatch, capsys):
    """The one case that must exit non-zero: bytes that no longer hash to their own name."""
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    blob = next((home / "store").iterdir())
    blob.write_bytes(b"tampered")
    capsys.readouterr()
    assert cli.main(["verify"]) == 1
    assert "ALTERED" in capsys.readouterr().out


def test_verify_treats_missing_bytes_as_a_takedown_rather_than_a_failure(home, monkeypatch, capsys):
    """Removing content on request keeps the record. That is deliberate, so it is not an error."""
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    next((home / "store").iterdir()).unlink()
    capsys.readouterr()
    assert cli.main(["verify"]) == 0
    assert "missing" in capsys.readouterr().out


def test_status_counts_captures_and_refusals(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    cli.main(["capture", "http://localhost/admin"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "captured  1" in out
    assert "declined  1" in out


def test_status_and_verify_say_so_when_there_is_no_library(home, capsys):
    assert cli.main(["status"]) == 2
    assert "no library" in capsys.readouterr().out


def test_promotable_lists_only_identified_works(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://arxiv.org/abs/2211.00593"])
    cli.main(["capture", "https://example.com/blog"])
    capsys.readouterr()
    assert cli.main(["promotable"]) == 0
    out = capsys.readouterr().out
    assert "arxiv-2211-00593" in out
    assert "1 of 2" in out


def test_the_digest_promotable_prints_is_one_promote_accepts(home, monkeypatch, tmp_path, capsys):
    """A tool that prints sixteen characters and demands sixty-four disagrees with itself.
    This is the test that would have caught it."""
    serve(monkeypatch)
    cli.main(["capture", "https://arxiv.org/abs/2211.00593"])
    capsys.readouterr()
    cli.main(["promotable"])
    printed = capsys.readouterr().out

    digest = next(
        line.strip()
        for line in printed.splitlines()
        if len(line.strip()) == 16 and all(c in "0123456789abcdef" for c in line.strip())
    )
    assert cli.main(["promote", digest, "--into", str(tmp_path / "citations")]) == 0


def test_promote_refuses_a_page_that_names_no_work(home, monkeypatch, tmp_path, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/blog"])
    digest = next((home / "store").iterdir()).name
    capsys.readouterr()
    assert cli.main(["promote", digest, "--into", str(tmp_path / "citations")]) == 1
    assert "no DOI or arXiv id" in capsys.readouterr().out


def test_hook_prints_a_configuration_that_parses(capsys):
    """It is meant to be pasted into settings.json, so it has to be valid JSON."""
    assert cli.main(["hook"]) == 0
    printed = capsys.readouterr().out
    config = json.loads(printed[: printed.index("\n\n")])
    assert config["hooks"]["PostToolUse"][0]["matcher"] == "WebFetch"


def test_hook_prints_the_entry_that_reports_beside_the_one_that_captures(capsys):
    cli.main(["hook"])
    printed = capsys.readouterr().out
    capturing, reporting = json.loads(printed[: printed.index("\n\n")])["hooks"]["PostToolUse"]
    assert capturing["hooks"] == [{"type": "command", "command": "retrieved-hook"}]
    assert reporting["matcher"] == "*"
    assert reporting["hooks"] == [{"type": "command", "command": "retrieved-hook --report"}]


def test_no_command_prints_help_rather_than_failing(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_capture_records_the_prompt_it_was_given(home, monkeypatch):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/", "--prompt", "what does it claim?"])
    import yaml

    record = yaml.safe_load(next((home / "retrievals").iterdir()).read_text())
    assert record["prompt"] == "what does it claim?"


def test_a_library_resolves_from_the_environment(home, monkeypatch):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    assert Library.resolve().root == home.resolve()


def test_a_url_captured_moments_ago_is_declined_not_fetched_again(home, monkeypatch, capsys):
    """The CLI paces itself the way the hook does. A rate limit one entry point honours and
    another ignores is not a rate limit -- and a script in a loop uses the entry point that
    ignores it."""
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/x"])
    capsys.readouterr()
    assert cli.main(["capture", "https://example.com/x"]) == 1
    out = capsys.readouterr().out
    assert "declined" in out
    assert "ago" in out
    assert len(list((home / "retrievals").glob("*.yaml"))) == 1


def test_a_second_host_request_too_soon_is_declined(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/a"])
    capsys.readouterr()
    assert cli.main(["capture", "https://example.com/b"]) == 1
    assert "minimum interval" in capsys.readouterr().out


def test_now_captures_a_page_the_rate_limit_would_have_held(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/a"])
    capsys.readouterr()
    assert cli.main(["capture", "https://example.com/b", "--now"]) == 0
    assert len(list((home / "retrievals").glob("*.yaml"))) == 2


def test_history_shows_each_capture_and_marks_a_changed_reading(home, monkeypatch, capsys):
    serve(monkeypatch, body=b"<html><body>first reading of the page</body></html>")
    cli.main(["capture", "https://example.com/p"])
    serve(monkeypatch, body=b"<html><body>second, different reading</body></html>")
    cli.main(["capture", "https://example.com/p", "--now"])
    capsys.readouterr()

    assert cli.main(["history", "https://example.com/p"]) == 0
    out = capsys.readouterr().out
    assert "2 captures, 2 distinct readings" in out
    assert "changed" in out
    assert not any(line.rstrip() != line for line in out.splitlines())


def test_history_of_a_single_capture_says_drift_is_not_measurable(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/p"])
    capsys.readouterr()
    assert cli.main(["history", "https://example.com/p"]) == 0
    assert "one capture, which cannot show drift" in capsys.readouterr().out


def test_history_of_a_url_never_captured_says_so(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    capsys.readouterr()
    assert cli.main(["history", "https://elsewhere.example.org/"]) == 2
    assert "never captured" in capsys.readouterr().out


def test_the_index_handle_is_closed_when_its_block_ends(home):
    """`with sqlite3.connect(...) as db` commits and does not close: it is a transaction manager
    wearing the shape of a resource manager. Code that looks like it releases the handle holds
    it, which on a locking filesystem is a second process that cannot write."""
    library = Library.resolve().create()
    with library.connect() as db:
        db.execute("SELECT COUNT(*) FROM retrievals").fetchone()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


def test_what_a_block_wrote_is_committed_by_the_time_it_ends(home):
    """Closing must not cost the commit the old form gave for free."""
    library = Library.resolve().create()
    with library.connect() as db:
        db.execute(
            "INSERT INTO retrievals VALUES ('d','https://e.example/','https://e.example/',"
            "'2026-01-01T00:00:00+00:00',200,'t','')"
        )
    assert library.history("https://e.example/")


MINUTES = (
    b"<html><body><p>The committee met on the second Tuesday of the month in the upstairs "
    b"room of the library, with eleven members present and two apologies received.</p>"
    b"<p>It agreed to repair the clock on the east wall before the winter, at a cost not to "
    b"exceed three hundred pounds, and to ask the school to paint the railings.</p>"
    b"<p>The treasurer reported a balance of four hundred and twelve pounds, and the meeting "
    b"closed at nine with a vote of thanks to the chair.</p></body></html>"
)


def checked_capture(n, said):
    def handler(request):
        return httpx.Response(
            200, content=MINUTES + f"<!--{n}-->".encode(), headers={"content-type": "text/html"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = capture.fetch(f"https://example.com/minutes/{n}", client=c)
    library = Library.resolve().create()
    outcome = answer.check(said, r)
    library.write(r, session_id="s1", answer_check=outcome)
    if outcome.flagged:
        library.flag(r, outcome, session_id="s1")


def test_status_reports_flags_and_how_many_answers_were_and_were_not_checked(
    home, monkeypatch, capsys
):
    checked_capture(1, 'It agreed "to repair the clock on the east wall before the winter".')
    checked_capture(2, 'It agreed "to replace the clock on the east wall before the winter".')
    checked_capture(3, "It agreed to repair a clock.")
    serve(monkeypatch)
    cli.main(["capture", "https://example.org/by-hand"])
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "answers   1 flagged   2 checked   1 not run   1 with no check recorded" in out
    assert "0 more checked against a stored copy   0 not run" in out
    assert "captured  4" in out


def test_status_counts_checks_against_stored_copies_without_counting_a_page_twice(
    home, monkeypatch, capsys
):
    checked_capture(1, 'It agreed "to repair the clock on the east wall before the winter".')
    library = Library.resolve()
    copy = library.latest("https://example.com/minutes/1")
    wrong = 'It agreed "to replace the clock on the east wall before the winter".'
    outcome = answer.check(wrong, copy)
    library.checked_stored(copy, outcome, declined="captured 20s ago", session_id="s1")
    library.flag(copy, outcome, session_id="s1", stored_copy=True)
    library.checked_stored(copy, answer.check(None, copy), declined="captured 30s ago")

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "captured  1" in out
    assert "answers   1 flagged   1 checked   0 not run   0 with no check recorded" in out
    assert "1 more checked against a stored copy   1 not run   (see answer_checks.jsonl)" in out
    assert "0 answers recorded as longer than their page's text" in out


def test_status_counts_a_long_answer_over_an_empty_page_apart_from_the_flags(
    home, monkeypatch, capsys
):
    def handler(request):
        return httpx.Response(
            200,
            content=b"<html><body>Sign in.</body></html>",
            headers={"content-type": "text/html"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = capture.fetch("https://example.com/minutes", client=c)
    outcome = answer.check("The committee met on the second Tuesday of the month. " * 20, r)
    assert outcome.answer_without_page_text and not outcome.flagged
    Library.resolve().create().write(r, answer_check=outcome)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "answers   0 flagged   0 checked   1 not run   0 with no check recorded" in out
    assert (
        "1 answers recorded as longer than their page's text   (recorded only, never flagged)"
        in out
    )


def test_status_counts_no_flags_in_a_library_that_predates_them(home, monkeypatch, capsys):
    serve(monkeypatch)
    cli.main(["capture", "https://example.com/"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert "answers   0 flagged   0 checked   0 not run   1 with no check recorded" in (
        capsys.readouterr().out
    )


def test_records_with_and_without_an_answer_check_verify_and_promote_alike(
    home, monkeypatch, tmp_path, capsys
):
    def handler(request):
        return httpx.Response(200, content=MINUTES, headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = capture.fetch("https://arxiv.org/abs/2211.00593", client=c)
    library = Library.resolve().create()
    said = 'It agreed "to replace the clock on the east wall before the winter".'
    library.write(r, answer_check=answer.check(said, r))
    serve(monkeypatch)
    cli.main(["capture", "https://arxiv.org/abs/2310.02207"])
    capsys.readouterr()

    assert cli.main(["verify"]) == 0
    assert "2 retrievals   2 intact   0 missing   0 altered" in capsys.readouterr().out
    assert cli.main(["promotable"]) == 0
    assert "2 of 2 retrievals name a work" in capsys.readouterr().out
    assert cli.main(["promote", r.bytes_sha256, "--into", str(tmp_path / "citations")]) == 0


def test_verify_counts_a_record_kept_without_bytes_apart_from_bytes_that_are_gone(
    home, monkeypatch, capsys
):
    library = Library.resolve().create()

    def page(n):
        def handler(request):
            return httpx.Response(
                200, content=f"<p>page {n}</p>".encode(), headers={"content-type": "text/html"}
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            return capture.fetch(f"https://example.com/{n}", client=c)

    library.write(page(1))
    library.write(page(2), keep_bytes=False)
    gone = page(3)
    library.write(gone)
    (library.store / gone.bytes_sha256).unlink()

    assert cli.main(["verify"]) == 0
    out = capsys.readouterr().out
    assert "3 retrievals   1 intact   1 missing   0 altered   1 recorded without bytes" in out
    assert out.count("bytes are gone") == 1
