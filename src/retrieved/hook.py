"""Capture a page the agent just fetched, as a Claude Code PostToolUse hook.

The agent's fetch tool does not hand back the page. Its response carries a byte count, a status
and a model's answer to whatever prompt accompanied the fetch -- so there is nothing here to keep
except the URL. This takes that URL and fetches it again, independently, which is why the record
says what it says: these are the bytes this library retrieved, not the bytes the agent read.

The response does carry the model's answer, and once the page is here the answer can be checked
against it: the passages it presents as quotations are on the page or they are not. The outcome
goes into the record. `retrieved-hook --report`, registered for every tool, is how a mismatch
reaches the agent, because the capture is detached and has nobody to tell by the time it knows.

Design constraints, in order, and each one is why a hook survives long enough to be useful:

1. Never break the session. Every failure exits 0 in silence.
2. Never block. `PostToolUse` cannot deny a call, so refusals here are records, not preventions.
   Anything that must not happen at all belongs in a `PreToolUse` guard.
3. Say nothing, unless an answer did not match its page, and then say it once. A hook that
   prints on every fetch is noise, and noise gets uninstalled. Failures and refusals go to
   `skipped.jsonl`, where they can be read on purpose.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Standard library only, by that module's own first constraint, so importing it here costs the
# fast path nothing and cannot fail on an installation missing a dependency.
from retrieved import flags

MAX_STDIN = 4 * 1024 * 1024


def _detach() -> int:
    """Pass stdin to a background copy and return, so the session never waits on a fetch.

    Everything here is best-effort by design. If the spawn fails there is no capture and no
    error: a hook that cannot break a session is worth more than a hook that captures everything,
    because the second one gets uninstalled the first time it hangs.
    """
    import subprocess  # noqa: PLC0415 - kept off the fast path's import cost

    try:
        payload = sys.stdin.read(MAX_STDIN)
    except OSError:
        return 0
    try:
        worker = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "retrieved.hook", "--capture"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        worker.stdin.write(payload.encode())
        worker.stdin.close()
    except (OSError, ValueError):
        return 0
    return 0


def _report() -> int:
    """Tell the agent about answers that did not match their pages, once each, or say nothing.

    Runs after every tool call, so it does as little as it can: read the payload, read one small
    file, and print only where this session has a flag nobody has been told about. The shape is
    the one Claude Code reads from a `PostToolUse` hook, `hookSpecificOutput.additionalContext`,
    which reaches the agent beside the tool result and is not shown as a message.
    """
    try:
        payload = json.loads(sys.stdin.read(MAX_STDIN))
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session = payload.get("session_id")
    cwd = payload.get("cwd")
    if not isinstance(session, str):
        return 0

    try:
        root = flags.library_root(pathlib.Path(cwd) if isinstance(cwd, str) and cwd else None)
        notes = flags.undelivered(root, session, _agent(payload))
    except Exception:  # noqa: BLE001 - constraint 1 outranks knowing which error
        return 0
    if not notes:
        return 0

    # The event name is echoed rather than assumed, because the field is required to name the
    # event the hook ran for and this may be registered on more than one.
    event = payload.get("hook_event_name")
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event if isinstance(event, str) and event else "PostToolUse",
                    "additionalContext": "\n".join(notes),
                }
            }
        )
    )
    return 0


def _agent(payload: dict) -> str:
    """Which subagent a hook fired in, or "" for the main agent.

    A subagent's tool calls carry the session's `session_id` and an `agent_id` of their own.
    Anything that is not a string is no agent, which is also what every payload held before
    the field existed.
    """
    agent = payload.get("agent_id")
    return agent if isinstance(agent, str) else ""


def _checked(answer, payload: dict, retrieval):
    """The answer in a payload checked against a retrieval, whatever the check does.

    A check that fails is recorded as one that did not run, with why: it must not cost the
    capture, and it must not leave a record that reads as though nothing was wrong.
    """
    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else ""
    response = payload.get("tool_response")
    tool_status = response.get("code") if isinstance(response, dict) else None
    try:
        return answer.check(_answer(payload), retrieval, prompt=prompt, tool_status=tool_status)
    except Exception as error:  # noqa: BLE001 - constraint 1 outranks knowing which error
        return answer.AnswerCheck(answer.NOT_RUN, reason=f"check failed: {type(error).__name__}")


def _answer(payload: dict) -> object:
    """The fetch tool's answer, or None where the payload does not hold one.

    `tool_response` has been seen as an object with a `result`. Anything else -- a bare string,
    a list, nothing -- is a shape this was not measured against, and guessing which part of it
    is the answer would check a quotation against text nobody claimed was one.
    """
    response = payload.get("tool_response")
    return response.get("result") if isinstance(response, dict) else None


def main() -> int:
    if "--report" in sys.argv:
        return _report()

    # A PostToolUse hook runs synchronously: the session waits for it. Fetching inline therefore
    # adds a second HTTP round-trip to every fetch the agent makes, and a hanging page stalls the
    # editing session for the whole timeout. So the default is to hand the payload to a detached
    # copy of this module and return immediately; `--capture` is that copy, doing the work with
    # nobody waiting.
    if "--capture" not in sys.argv:
        return _detach()

    try:
        payload = json.loads(sys.stdin.read(MAX_STDIN))
    except (ValueError, OSError):
        return 0

    # `null` and `[]` are valid JSON and are not objects. Parsing successfully is not the same as
    # receiving a payload, and the difference is an exception in someone's editing session.
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    url = tool_input.get("url") or ""
    if not url:
        return 0

    # Imported here, and only here, for the reason the constraint list gives: an installation
    # missing a dependency must produce a silent no-op rather than an exception in someone's
    # editing session. Everything above this line runs on the standard library alone.
    try:
        from retrieved import answer, politeness
        from retrieved.capture import USER_AGENT, fetch
        from retrieved.refuse import Refused, check
        from retrieved.store import Library
    except ImportError:
        return 0

    session = payload.get("session_id") or ""
    agent = _agent(payload)
    cwd = payload.get("cwd")
    library = Library.resolve(pathlib.Path(cwd) if cwd else None)
    library.create()

    # Two different questions, asked in this order. The denylist answers what must never be
    # requested; politeness answers what should not be requested yet, again, or by this session.
    # Both refusals are recorded, because a store of successes alone cannot say why something is
    # missing. The order is not cosmetic: asked the other way, a session working against a local
    # server spends its whole capture budget on URLs that were never going to be fetched.
    try:
        check(url)
    except Refused as refusal:
        library.skip(refusal.url, refusal.reason, session_id=session)
        return 0

    try:
        with library.connect() as db:
            politeness.check(db, url, USER_AGENT, session_id=session)
    except politeness.Declined as declined:
        library.skip(declined.url, declined.reason, session_id=session)
        # Declined is about the request, and the answer is here whether or not one is made. So
        # where the library already holds the page, the answer is checked against that copy
        # with no request at all, and everything written says which copy it was. Where it holds
        # none, the skip above is the whole record and nothing is checked.
        try:
            copy = library.latest(url)
            if copy is not None:
                outcome = _checked(answer, payload, copy)
                library.checked_stored(
                    copy, outcome, declined=declined.reason, session_id=session, agent_id=agent
                )
                if outcome.flagged:
                    library.flag(
                        copy, outcome, session_id=session, agent_id=agent, stored_copy=True
                    )
        except Exception as error:  # noqa: BLE001 - constraint 1 outranks knowing which error
            library.skip(
                url, f"stored copy not checked: {type(error).__name__}", session_id=session
            )
        return 0
    except Exception as error:  # noqa: BLE001 - pacing must never break a capture
        # Proceed unpaced rather than not at all, and say so. A rate limiter that failed is a
        # different fact from one that declined, and swallowing it silently would leave a request
        # in the log that nothing explains.
        library.skip(
            url, f"pacing unavailable, fetched anyway: {type(error).__name__}", session_id=session
        )

    try:
        with library.connect() as db:
            politeness.record(db, url, session_id=session)
    except Exception as error:  # noqa: BLE001
        library.skip(url, f"pacing not recorded: {type(error).__name__}", session_id=session)

    try:
        retrieval = fetch(url)
    except Refused as refusal:
        library.skip(refusal.url, refusal.reason, session_id=session)
        return 0
    except Exception as error:  # noqa: BLE001 - constraint 1 outranks knowing which error
        library.skip(url, f"fetch failed: {type(error).__name__}", session_id=session)
        return 0

    # Checked here, with the page in hand and nobody waiting.
    outcome = _checked(answer, payload, retrieval)

    try:
        library.write(
            retrieval,
            session_id=session,
            agent_id=agent,
            prompt=tool_input.get("prompt") or "",
            answer_check=outcome,
        )
        if outcome.flagged:
            library.flag(retrieval, outcome, session_id=session, agent_id=agent)
    except OSError as error:
        library.skip(url, f"could not write: {error.strerror}", session_id=session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
