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
