"""The commands, each answering one question.

    retrieved capture <url>   keep what is at this URL now
    retrieved verify          do the stored bytes still hash to what the record says?
    retrieved status          what is captured, what drifted, what was declined
    retrieved history <url>   every capture of one URL, and where the reading changed
    retrieved promotable      which captures name a work a bibliography could hold
    retrieved promote <d>     put one of them into a citation library
    retrieved hook            the configuration that captures fetches automatically

`verify` checks the store against itself, not the store against the web. Whether a page still
says what it said is `history`, and it is only answerable for URLs captured more than once --
which is the honest limit of a tool that refuses to re-crawl on a timer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

from retrieved import politeness
from retrieved.capture import USER_AGENT, fetch
from retrieved.promote import CannotPromote, candidates, promote
from retrieved.refuse import Refused
from retrieved.refuse import check as denylist
from retrieved.store import Library

HOOK_CONFIG = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "WebFetch",
                "hooks": [{"type": "command", "command": "retrieved-hook"}],
            }
        ]
    }
}


def _refused(library: Library, refusal: Refused) -> int:
    library.skip(refusal.url, refusal.reason)
    print(f"  refused  {refusal.url}\n           {refusal.reason}")
    return 1


def cmd_capture(args: argparse.Namespace) -> int:
    library = Library.resolve().create()

    # The denylist first, and pacing second. They answer different questions -- what must never be
    # requested, then what should not be requested yet -- and in the other order a URL that will
    # never be fetched still spends a slot against the session cap. A session working against a
    # local server would exhaust its budget on requests that never happened.
    try:
        denylist(args.url)
    except Refused as refusal:
        return _refused(library, refusal)

    # The same pacing the hook applies. A rate limit that depends on which entry point was used
    # is not a rate limit: a script calling `retrieved capture` in a loop is exactly the burst
    # these limits exist to prevent, and it would have gone straight past them.
    if not args.now:
        try:
            with library.connect() as db:
                politeness.check(db, args.url, USER_AGENT)
        except politeness.Declined as declined:
            library.skip(declined.url, declined.reason)
            print(f"  declined {declined.url}\n           {declined.reason}")
            print("           --now captures anyway")
            return 1
    with library.connect() as db:
        politeness.record(db, args.url)

    # Checked again inside `fetch`, against the URL redirects actually reached.
    try:
        retrieval = fetch(args.url)
    except Refused as refusal:
        return _refused(library, refusal)
    path = library.write(retrieval, prompt=args.prompt or "")
    print(f"  captured {retrieval.final_url}")
    print(f"    status {retrieval.http_status}   {retrieval.bytes_len} bytes")
    print(f"    bytes  {retrieval.bytes_sha256[:16]}")
    print(f"    text   {retrieval.text_sha256[:16]}   {retrieval.extractor}")
    print(f"    record {path.relative_to(library.root.parent)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    library = Library.resolve()
    if not library.retrievals.is_dir():
        print(f"  no library at {library.root}")
        return 2

    checked = intact = missing = altered = 0
    for record in sorted(library.retrievals.glob("*.yaml")):
        checked += 1
        digest = record.stem
        blob = library.store / digest
        if not blob.is_file():
            missing += 1
            print(f"  missing  {digest[:16]}  bytes are gone; the record remains")
            continue
        if hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
            altered += 1
            print(f"  ALTERED  {digest[:16]}  stored bytes do not hash to their name")
            continue
        intact += 1

    print(f"\n  {checked} retrievals   {intact} intact   {missing} missing   {altered} altered")
    # Missing is not failure: a takedown removes bytes and keeps the record, deliberately.
    return 1 if altered else 0


def cmd_status(args: argparse.Namespace) -> int:
    library = Library.resolve()
    if not library.index_path.exists():
        print(f"  no library at {library.root}")
        return 2

    stored = len(list(library.retrievals.glob("*.yaml")))
    drifted = library.drifted()
    skipped = (
        len(library.skipped_path.read_text().strip().splitlines())
        if library.skipped_path.exists()
        else 0
    )

    print(f"  library   {library.root}")
    print(f"  captured  {stored}")
    print(f"  declined  {skipped}   (denylist or rate limit; see skipped.jsonl)")
    if drifted:
        print(f"\n  {len(drifted)} URLs read differently on a later fetch:")
        for url, fetches, readings in drifted[:10]:
            print(f"    {readings} readings over {fetches} fetches   {url[:70]}")
    else:
        print("  drifted   0   (only measurable for URLs captured more than once)")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Every capture of one URL, marking the fetches where the reading changed.

    Reported on the text digest. Bytes differ between almost any two fetches of a live page --
    session ids, ad slots, a timestamp in a footer -- so a list of byte digests would mark every
    row as changed and mean nothing by it.
    """
    library = Library.resolve()
    rows = library.history(args.url)
    if not rows:
        print(f"  never captured  {args.url}")
        return 2

    previous = ""
    for fetched_at, byte_digest, text_digest in rows:
        mark = "changed" if previous and text_digest != previous else ""
        print(f"  {fetched_at}  {byte_digest[:12]}  {text_digest[:12]}  {mark}".rstrip())
        previous = text_digest

    readings = len({row[2] for row in rows})
    if len(rows) == 1:
        print("\n  one capture, which cannot show drift; that needs a second")
    else:
        print(f"\n  {len(rows)} captures, {readings} distinct readings")
    return 0


def cmd_promotable(args: argparse.Namespace) -> int:
    """What could go into a bibliography. Nothing here has, or will without being asked."""
    library = Library.resolve()
    found = candidates(library)
    if not found:
        print("  nothing captured carries a DOI or an arXiv id")
        return 0
    for digest, slug, url in found:
        print(f"  {slug:<26}{url[:58]}\n  {'':<26}{digest[:16]}")
    total = len(list(library.retrievals.glob("*.yaml")))
    print(f"\n  {len(found)} of {total} retrievals name a work.")
    print("  retrieved promote <digest> --into <citations library>")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    library = Library.resolve()
    home = pathlib.Path(args.into).expanduser().resolve()
    try:
        path = promote(library, args.digest, home, force=args.force)
    except CannotPromote as refusal:
        print(f"  {refusal}")
        return 1
    print(f"  wrote {path.relative_to(home)} in {home}")
    print("  authors and year are blank: run `citations resolve` before citing it")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    print(json.dumps(HOOK_CONFIG, indent=2))
    print("\n  Add the PostToolUse entry to ~/.claude/settings.json.")
    print("  It captures pages the agent fetches; it never blocks a fetch, because")
    print("  PostToolUse cannot. Refusals are recorded in skipped.jsonl.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retrieved", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")

    capture = sub.add_parser("capture", help="keep what is at this URL now")
    capture.add_argument("url")
    capture.add_argument("--prompt", help="what was being asked of the page")
    capture.add_argument(
        "--now", action="store_true", help="capture without waiting out the rate limit"
    )
    capture.set_defaults(fn=cmd_capture)

    sub.add_parser("verify", help="do the stored bytes still hash to their names?").set_defaults(
        fn=cmd_verify
    )
    sub.add_parser("status", help="what is captured, drifted, declined").set_defaults(fn=cmd_status)

    history = sub.add_parser("history", help="every capture of one URL, and where it changed")
    history.add_argument("url")
    history.set_defaults(fn=cmd_history)

    sub.add_parser(
        "promotable", help="which captures name a work, and could join a bibliography"
    ).set_defaults(fn=cmd_promotable)

    prom = sub.add_parser("promote", help="put one capture into a citation library")
    prom.add_argument("digest")
    prom.add_argument("--into", required=True, help="the citation library to write into")
    prom.add_argument("--force", action="store_true", help="overwrite an existing record")
    prom.set_defaults(fn=cmd_promote)

    sub.add_parser("hook", help="print the hook configuration").set_defaults(fn=cmd_hook)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
