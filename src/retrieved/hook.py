"""Capture a page the agent just fetched, as a Claude Code PostToolUse hook.

The agent's fetch tool does not hand back the page. Its response carries a byte count, a status
and a model's answer to whatever prompt accompanied the fetch -- so there is nothing here to keep
except the URL. This takes that URL and fetches it again, independently, which is why the record
says what it says: these are the bytes this library retrieved, not the bytes the agent read.

Design constraints, in order, and each one is why a hook survives long enough to be useful:

1. Never break the session. Every failure exits 0 in silence.
2. Never block. `PostToolUse` cannot deny a call, so refusals here are records, not preventions.
   Anything that must not happen at all belongs in a `PreToolUse` guard.
3. Say nothing. A hook that prints on every fetch is noise, and noise gets uninstalled. Failures
   and refusals go to `skipped.jsonl`, where they can be read on purpose.
"""

from __future__ import annotations

import json
import pathlib
import sys

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


def main() -> int:
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
        from retrieved import politeness
        from retrieved.capture import USER_AGENT, fetch
        from retrieved.refuse import Refused, check
        from retrieved.store import Library
    except ImportError:
        return 0

    session = payload.get("session_id") or ""
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

    try:
        library.write(retrieval, session_id=session, prompt=tool_input.get("prompt") or "")
    except OSError as error:
        library.skip(url, f"could not write: {error.strerror}", session_id=session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
