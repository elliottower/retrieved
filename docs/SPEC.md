# What a retrieval record means

A record in `retrievals/` is one assertion, and it is narrower than it looks.

> These bytes, whose SHA-256 is `<bytes_sha256>`, were returned by a GET of `<final_url>` at
> `<fetched_at>`, by this library, with no browser and no script execution.

Everything else in the record supports that assertion or qualifies it. Nothing in it claims the
bytes are what any agent read, that the page is authoritative, that the extraction is correct, or
that a passage in it supports anything.

## Fields

| field | what it is for |
|---|---|
| `url` | what was asked for |
| `final_url` | what answered, after redirects. The denylist is checked against this too |
| `fetched_at` | ISO 8601, UTC, second precision |
| `http_status` | 200 is not success; see the asymmetry below |
| `content_type` | decides whether markup is stripped before the text digest |
| `bytes` | length of what was stored, after truncation |
| `bytes_sha256` | names the file in `store/`, and is the record's identity |
| `text_sha256` | digest of the extracted text; the one that answers whether a page changed |
| `extractor` | which normalisation produced that text, and therefore what the digest means |
| `rendering_method` | `httpx, no browser` |
| `javascript_executed` | always false today, recorded because a JS shell and an empty page are otherwise indistinguishable |
| `response_headers` | credentials removed; kept for a later WARC export |
| `truncated` | whether the response exceeded the size cap |
| `prompt` | what the agent was asking of the page. Not a quotation |
| `session_id` | which agent session caused the capture |
| `note` | states in the record itself that this is an independent retrieval |
| `answer_check` | optional. Whether the passages the fetch tool's answer quoted are in this retrieval's text: `found`, `not_found` with the passages, or `not_run` with a reason. Holds the answer's digest, never the answer. `checked_against` names what produced the text they were looked for in |

`answer_check` is a statement about containment and nothing more. `found` means the quoted words
are in these bytes, not that the page supports what the answer made of them, and `not_found`
means they are not in this retrieval, which is not necessarily the page the fetch tool read. A
record without the block was never checked.

`checked_against` is not `extractor`. `extractor` names the normalisation `text_sha256` is taken
over, which decodes a PDF's bytes as they are and must never change. `checked_against` names
what the answer's passages were looked for in, which for a PDF is the output of `pdftotext` and
for JSON is its parsed string values. The first answers whether a page changed, the second
whether a sentence is in it, and a record carries both.

## Checks against a copy already held

A record is the account of one fetch, and its `answer_check` is the check of the answer that
fetch was made for. An answer about a URL the rate limiter declined has no fetch of its own.
Where the library holds an earlier copy, that answer is checked against it and the outcome is one
line of `answer_checks.jsonl`:

| field | what it is for |
|---|---|
| `at` | when the check was made |
| `session_id`, `agent_id` | whose fetch it was |
| `url` | what was asked for |
| `bytes_sha256` | which copy the answer was checked against |
| `copy_fetched_at` | when that copy was fetched, which is not when the answer was given |
| `not_fetched_because` | the rate limiter's reason, as in `skipped.jsonl`, where no request was made |
| `same_bytes_fetched_at` | when a request was made and returned the bytes the record already names. One of this and the field above is set |
| `answer_check` | the same block a record carries |

The file is append-only. Several answers checked against one copy are several lines, and a
record that carries a check is never rewritten: not by a check against it, and not by a later
fetch that returns the same bytes. The index row is replaced by that fetch, so the index gives
the last time the bytes came back and the record the first.

## What is told, and what is only recorded

`flags.jsonl` holds one thing: an answer that quoted a passage the page does not hold.
`answer_without_page_text` is recorded in the block and never flagged. A second fetch that came
back with an error status is `not_run`, whatever the answer said, and no passage is looked for
in an error page.

## Two digests

`bytes_sha256` answers *are these the same bytes*. On a live page it almost always says no:
session identifiers, CSRF tokens, ad slots, build stamps and timestamps all move between
requests. A drift number computed on it approaches 100% and means nothing.

`text_sha256` answers *does this page still say the same thing*, which is the question anyone
actually has. It is taken over text with scripts, styles and tags removed and whitespace
collapsed, and `extractor` names the procedure — because a digest of an extraction is meaningless
without knowing which extraction, and a changed extractor version changes the digest without the
page changing at all.

## The authentication asymmetry

The agent may have fetched with cookies. This library does not. So a capture can return **200 OK**
and contain a login wall, a paywall, or a consent interstitial — mechanically successful, and
containing nothing the agent read.

This is a real limitation, not a hypothetical. It is why `http_status` alone is never treated as
success, and why the honest reading of a record is "what this URL served an anonymous client at
that moment".

## What refusal means

A URL in `skipped.jsonl` was not captured, with a reason. That file exists because a store
holding only successes gives the same silence for *there was nothing to capture* and *capture was
declined*, and those are different facts.

Refusal is recorded and never enforced by the hook: `PostToolUse` cannot block a call. Preventing
a fetch requires a `PreToolUse` guard, which is a separate thing this package does not yet ship.

## Stability

`bytes_sha256` is the record's identity and will not change meaning. `text_sha256` depends on
the extractor, so its value may change across versions of this package; `extractor` is recorded
in every record precisely so that a change is visible rather than silent.
