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


def main() -> int:
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
        from retrieved.capture import fetch
        from retrieved.refuse import Refused
        from retrieved.store import Library
    except ImportError:
        return 0

    session = payload.get("session_id") or ""
    cwd = payload.get("cwd")
    library = Library.resolve(pathlib.Path(cwd) if cwd else None)

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
