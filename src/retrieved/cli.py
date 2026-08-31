"""The commands.

Four, each answering one question:

    retrieved capture <url>   keep what is at this URL now
    retrieved verify          do the stored bytes still hash to what the record says?
    retrieved status          what is captured, what drifted, what was declined
    retrieved hook            the configuration that captures fetches automatically

`verify` checks the store against itself, not the store against the web. Whether a page still
says what it said is `status`, and it is only answerable for URLs captured more than once --
which is the honest limit of a tool that refuses to re-crawl on a timer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

from retrieved.capture import fetch
from retrieved.promote import CannotPromote, candidates, promote
from retrieved.refuse import Refused
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


def cmd_capture(args: argparse.Namespace) -> int:
    library = Library.resolve()
    try:
        retrieval = fetch(args.url)
    except Refused as refusal:
        library.skip(refusal.url, refusal.reason)
        print(f"  refused  {refusal.url}\n           {refusal.reason}")
        return 1
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
    capture.set_defaults(fn=cmd_capture)

    sub.add_parser("verify", help="do the stored bytes still hash to their names?").set_defaults(
        fn=cmd_verify
    )
    sub.add_parser("status", help="what is captured, drifted, declined").set_defaults(fn=cmd_status)
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
