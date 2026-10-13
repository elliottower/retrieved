"""What the answer check raised, and telling the agent about it once.

A capture is detached: by the time the page has been fetched and the answer checked against it,
the hook call that started the work returned long ago. So a mismatch cannot ride back on that
call. It is written to `flags.jsonl`, and a second hook, registered for every tool, delivers it
on the next call the same session makes.

    <library>/
        flags.jsonl          one line per answer that did not match its page; append only
        flags.delivered/     one empty file per flag the agent has been told about

A session is not one reader. A subagent's tool calls run the same hooks with the session's own
`session_id` and an `agent_id` beside it, so a flag keyed by session alone is delivered to
whichever agent calls a tool next, and that is as likely to be a second subagent that never saw
the page. A flag therefore records which agent's fetch raised it. A subagent is told about its
own; the main agent is told about its own and any subagent's, because it is the reader of what
a subagent reports and a subagent that has finished makes no further call to be told on.

Design constraints, in order:

1. Standard library only. This runs after every tool call an agent makes, so it is imported on
   the hot path of a session, and the rest of this package costs an HTTP client and a YAML
   parser to import. Nothing here may import a module that does.
2. Say a thing once. A note repeated on every tool call is noise, and noise gets uninstalled.
3. Never twice, even under concurrency. Tool calls made in parallel run their hooks in parallel,
   so two processes can see the same undelivered flag in the same instant.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib

DEFAULT_DIRNAME = ".retrieved"
FLAGS_NAME = "flags.jsonl"
DELIVERED_NAME = "flags.delivered"

#: Passages named in one note. The rest are counted, and all of them are in `flags.jsonl`.
NOTE_PASSAGES = 5

#: Said in every note, because it changes what the note means: a passage missing from this
#: retrieval may have been on the page the fetch tool read.
INDEPENDENT = (
    "The page was fetched again, independently, and a second fetch can differ from what the "
    "fetch tool read."
)

#: Said to the main agent about a fetch it did not make, so the URL is not a puzzle to it.
BY_A_SUBAGENT = "The fetch was made by a subagent of this session."


def library_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Where the library is, without importing what reads one.

    `$RETRIEVED_HOME`, else a `.retrieved/` found by walking up from `start`, else one beside
    it. `Library.resolve` is this function, so the two hooks cannot disagree about which
    library a session writes to.
    """
    if home := os.environ.get("RETRIEVED_HOME"):
        return pathlib.Path(home).expanduser().resolve()
    here = (start or pathlib.Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / DEFAULT_DIRNAME
        if candidate.is_dir():
            return candidate
    return here / DEFAULT_DIRNAME


def raised(root: pathlib.Path) -> list[dict]:
    """Every flag in the library, oldest first, each with the `id` it is delivered under.

    The id is a digest of the line as written. The file is append-only, so a line never changes
    and needs no identifier stored in it. A line that is not a JSON object is passed over: one
    damaged line must not hide the flags after it.
    """
    try:
        lines = (root / FLAGS_NAME).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    flags = []
    for line in lines:
        try:
            flag = json.loads(line)
        except ValueError:
            continue
        if isinstance(flag, dict):
            flags.append({**flag, "id": hashlib.sha256(line.encode()).hexdigest()[:32]})
    return flags


def _stored_copy(flag: dict) -> str:
    """When the copy a flag was checked against was fetched, or "" where it was a new fetch.

    Read back as a timestamp and written out again, so what reaches the agent is a time this
    function formatted and never a string taken from a file on trust.
    """
    try:
        fetched = datetime.datetime.fromisoformat(flag.get("copy_fetched_at") or "")
    except (TypeError, ValueError):
        return ""
    return fetched.isoformat(timespec="seconds")


def note(flag: dict) -> str:
    """One flag as the agent reads it: what was measured, and which retrieval it was measured on.

    Only the URL, counts, passages from the fetch tool's own answer and the time of a stored
    copy go in. Nothing from the stored page does: the page is untrusted, and a note is read by
    the agent as an instruction's neighbour.
    """
    url = flag.get("url") or "a page"
    if copy := _stored_copy(flag):
        # A check made without a request, against the copy the library already held. The note
        # must not read as though the page had just been fetched, because it had not.
        where = f"the copy of the page this library holds, fetched {copy}"
        caveat = (
            f"The page was not fetched again for this check: the copy is from {copy}, and it "
            f"can differ from what the fetch tool read."
        )
    else:
        where, caveat = "the page as retrieved", INDEPENDENT
    passages = [str(passage) for passage in flag.get("not_found") or []]
    shown = ", ".join(f'"{passage}"' for passage in passages[:NOTE_PASSAGES])
    if len(passages) > NOTE_PASSAGES:
        shown += f", and {len(passages) - NOTE_PASSAGES} more"
    counted = "1 passage" if len(passages) == 1 else f"{len(passages)} passages"
    verb = "is" if len(passages) == 1 else "are"
    return (
        f"retrieved: {counted} the fetch answer quoted from {url} {verb} not in {where}: "
        f"{shown}. Read the page before quoting it. {caveat}"
    )


def _claim(root: pathlib.Path, flag_id: str) -> bool:
    """Mark one flag delivered. True for exactly one caller, however many ask at once.

    Creating a file that must not exist is atomic, which a read of a list followed by an append
    to it is not: two hooks running for two parallel tool calls would both read "undelivered"
    and both speak. A marker that cannot be written is treated as already claimed, because
    silence costs one note and the alternative is the same note on every call.
    """
    try:
        (root / DELIVERED_NAME).mkdir(parents=True, exist_ok=True)
        os.close(os.open(root / DELIVERED_NAME / flag_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return False
    return True


def _is_for(flag: dict, session_id: str, agent_id: str) -> bool:
    """Whether this caller is one a flag should be delivered to.

    A subagent hears about its own fetches. The main agent, which has no `agent_id`, hears about
    every flag of its session. A flag written before agents were recorded has no `agent_id`,
    reads as the main agent's, and is delivered as it always was.
    """
    if flag.get("session_id") != session_id:
        return False
    # A line with no passage is from a release that also flagged a long answer over a page with
    # almost no text. That is recorded now and no longer told, and a line already written is
    # not told either.
    if not flag.get("not_found"):
        return False
    return not agent_id or (flag.get("agent_id") or "") == agent_id


def undelivered(root: pathlib.Path, session_id: str, agent_id: str = "") -> list[str]:
    """Notes for this caller's flags that nobody has been told about, each now marked as told.

    Marked before it is returned rather than after it is printed. A hook killed between the two
    loses a note, and the flag is still in `flags.jsonl` and in `retrieved status`; the other
    order delivers it twice.
    """
    if not session_id:
        return []
    notes = []
    for flag in raised(root):
        if _is_for(flag, session_id, agent_id) and _claim(root, flag["id"]):
            from_elsewhere = not agent_id and flag.get("agent_id")
            notes.append(f"{note(flag)} {BY_A_SUBAGENT}" if from_elsewhere else note(flag))
    return notes
