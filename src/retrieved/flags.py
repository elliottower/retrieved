"""What the answer check raised, and telling the agent about it once.

A capture is detached: by the time the page has been fetched and the answer checked against it,
the hook call that started the work returned long ago. So a mismatch cannot ride back on that
call. It is written to `flags.jsonl`, and a second hook, registered for every tool, delivers it
on the next call the same session makes.

    <library>/
        flags.jsonl          one line per answer that did not match its page; append only
        flags.delivered/     one empty file per flag the agent has been told about

Design constraints, in order:

1. Standard library only. This runs after every tool call an agent makes, so it is imported on
   the hot path of a session, and the rest of this package costs an HTTP client and a YAML
   parser to import. Nothing here may import a module that does.
2. Say a thing once. A note repeated on every tool call is noise, and noise gets uninstalled.
3. Never twice, even under concurrency. Tool calls made in parallel run their hooks in parallel,
   so two processes can see the same undelivered flag in the same instant.
"""

from __future__ import annotations

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


def note(flag: dict) -> str:
    """One flag as the agent reads it: what was measured, and that the retrieval was a re-fetch.

    Only the URL, counts and passages from the fetch tool's own answer go in. Nothing from the
    stored page does: the page is untrusted, and a note is read by the agent as an instruction's
    neighbour.
    """
    url = flag.get("url") or "a page"
    if flag.get("answer_without_page_text"):
        return (
            f"retrieved: the fetch answer for {url} runs to {flag.get('answer_chars')} "
            f"characters, and the page as retrieved holds {flag.get('page_text_chars')} "
            f"characters of text (HTTP {flag.get('http_status')}). The answer may not come from "
            f"the page. {INDEPENDENT}"
        )
    passages = [str(passage) for passage in flag.get("not_found") or []]
    shown = ", ".join(f'"{passage}"' for passage in passages[:NOTE_PASSAGES])
    if len(passages) > NOTE_PASSAGES:
        shown += f", and {len(passages) - NOTE_PASSAGES} more"
    counted = "1 passage" if len(passages) == 1 else f"{len(passages)} passages"
    verb = "is" if len(passages) == 1 else "are"
    return (
        f"retrieved: {counted} the fetch answer quoted from {url} {verb} not in the page as "
        f"retrieved: {shown}. Read the page before quoting it. {INDEPENDENT}"
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


def undelivered(root: pathlib.Path, session_id: str) -> list[str]:
    """Notes for this session's flags that nobody has been told about, each now marked as told.

    Marked before it is returned rather than after it is printed. A hook killed between the two
    loses a note, and the flag is still in `flags.jsonl` and in `retrieved status`; the other
    order delivers it twice.
    """
    if not session_id:
        return []
    return [
        note(flag)
        for flag in raised(root)
        if flag.get("session_id") == session_id and _claim(root, flag["id"])
    ]
