# retrieved

Keep the bytes an agent fetched, so a claim about a page can be checked against the page.

```bash
uv tool install retrieved
retrieved capture https://example.com/
retrieved verify
```

## The problem

An agent's fetch tool does not hand the page to the agent. It fetches the bytes, has a separate
model read them, and returns that model's answer:

```text
  agent calls WebFetch(url, prompt)
        │
        ▼
  bytes fetched ────────────────────► 559 bytes, HTTP 200
        │
        ▼
  a separate model reads them, answering the prompt      ◄── the page ends here
        │
        ▼
  the agent receives one string:  "Example Domain"
```

Measured, not inferred: a `PostToolUse` hook on `WebFetch` receives
`{"bytes": 559, "code": 200, "result": "Example Domain"}`. A byte count, and an answer. Never the
bytes.

So three failures are indistinguishable from a good answer, and all three are silent: the reader
model compressed away the qualifier; the page was a login wall or a JavaScript shell returning
200 OK; or the page did not answer the question and the model answered anyway. Nothing is written
down, so nothing can be checked afterwards.

## What this does

Takes the URL, fetches it again independently, and keeps what came back.

```text
  .retrieved/
      store/<bytes_sha256>            the bytes
      retrievals/<bytes_sha256>.yaml  url, both digests, status, headers, when
      index.db                        one row per retrieval
      skipped.jsonl                   what was not captured, and why
      flags.jsonl                     answers that did not match the page they were about
      answer_checks.jsonl             answers checked against a copy already held
```

**Two digests, because one is useless.** HTML changes on every request — session ids, CSRF
tokens, ad slots — so a byte digest answers *are these the same bytes* and almost always says no.
The text digest, taken over the extracted text with the extractor named beside it, answers the
question worth asking: **does this page still say the same thing?**

**What a record claims, exactly.** That these bytes were at this URL at this time, retrieved by
this library. Not that they are the bytes the agent read. A re-fetch can differ, and a page served
conditionally can differ on purpose. Every record says so in its own `note` field rather than
leaving it to a README.

## Automatic capture

```bash
retrieved hook            # prints the config; add it to ~/.claude/settings.json
```

Pages the agent fetches are then captured as a byproduct of ordinary work, with no manifest to
author. The capturing hook never blocks a fetch — `PostToolUse` cannot — and never prints, because
a hook that speaks on every fetch gets uninstalled. Refusals land in `skipped.jsonl`.

### Keeping the record without the page

By default every page the hook fetches is stored. `RETRIEVED_KEEP` in the hook's command changes
that:

| `RETRIEVED_KEEP` | bytes stored |
|---|---|
| `all` (default) | every page |
| `flagged` | only a page whose answer quoted a passage the page does not hold |
| `none` | no page |

    "command": "RETRIEVED_KEEP=flagged retrieved-hook"

The record is written whichever it is: the URL, the time, the status, the length, both digests
and the check of the answer, with `bytes_kept: false` where the page was not stored. That is
enough to say later whether the page still reads the same, and to show which passage of an
answer was not on it. It is not enough to read the page again, and it costs one thing more: a
URL fetched twice within fifteen minutes is checked against the stored copy, and where no copy
was kept the second answer is not checked. A value that is none of the three keeps every page.
`retrieved capture` always keeps the bytes.

## Checking the answer

The capture has the page and the hook payload has the answer, so the one can be checked against
the other. Passages the answer puts in double quotation marks or in a markdown block quote,
thirty characters or longer, are looked for in the text of the page as retrieved.

```text
  answer:  The docs say "requests are retried three times before failing."
  page:    ...requests are retried twice before failing, and the error is...

  retrieved: 1 passage the fetch answer quoted from https://example.com/docs is not in the
  page as retrieved: "requests are retried three times before failing.". Read the page before
  quoting it. The page was fetched again, independently, and a second fetch can differ from
  what the fetch tool read.
```

The comparison ignores what typesetting adds and keeps which words appear: case, curly against
straight marks, the several dashes, HTML entities, soft hyphens and runs of whitespace are folded
on both sides, and a full stop or comma closing the quotation is not held against it. A passage
with an ellipsis or square brackets was edited by whoever quoted it, and is not checked.

A passage that comparison misses is looked for once more with spaces, quotation marks and
markdown's emphasis marks removed from both sides. A tag inside a word (`keeper<b>s</b>`) reads
as a space on the page, the fetch tool reads `<em>` as `_`, and an answer nesting a quotation
writes single marks where the page has double. Letters, digits, full stops and minus signs stay,
so no other word or number gets through. What is given up is word boundaries, over thirty
characters or more.

Three kinds of quoted string are not looked for at all, because none is a claim about the page:
words that are in the prompt (a title the agent asked after, or the whole prompt repeated in the
fetch tool's notice of a redirect), an attribute's value in markup the answer reproduces
(`alt="..."`), and a block quote that holds a heading, a list or a table.

What the page's text is depends on what the page is, and the record names it under
`checked_against`:

| page | checked against | `checked_against` |
|---|---|---|
| HTML | its text with tags removed, and the bytes as served | `retrieved.normalize/html` |
| `text/*` | the text as it is | `retrieved.normalize/raw` |
| JSON | its string values, parsed, so `\"` and `\u2019` are the characters | `retrieved.extract/json` |
| XML, Atom | its text with tags removed | `retrieved.extract/xml` |
| PDF | what `pdftotext` extracts, where it is installed | `pdftotext 24.02.0` |

`pdftotext` is part of poppler (`brew install poppler`, `apt install poppler-utils`). It is run
without `-layout`, in reading order, on the stored bytes, with a timeout. It rejoins a word
hyphenated across a line end by dropping the hyphen, so for a PDF a hyphen between two letters
is ignored on both sides. None of this changes `text_sha256` or the record's `extractor`, which
describe the capture and not the check.

Every record written by the hook says which of three things happened, under `answer_check`:

| `status` | meaning |
|---|---|
| `found` | every quoted passage is in the page |
| `not_found` | some are not; the record lists them |
| `not_run` | nothing was checked, and `reason` says why |

`not_run` is not a pass. A second fetch the server answered with an error is one: a 403 or a 404
carries a page of its own, and a passage looked for there would be reported missing from a page
nobody quoted. Where the fetch tool's own request had succeeded the reason says so outright,
`the independent fetch was refused (HTTP 403); the page the tool read was not obtained`, which
is "could not check" and not a doubt about the answer. A PDF with no `pdftotext` installed, one it could not read or did not
finish in twenty seconds, and one with no text layer are each `not_run` with that as the reason,
and never a passage reported missing. So are JSON that does not parse, a ZIP or an image, a
truncated page, which would report its last passages missing, a page with almost no text, which
is a wall or a shell and not the article, and an answer with no quotation in it. A record with no
`answer_check` at all predates the check or was captured by hand.

One more thing is measured, separately from quotations: an answer of 800 characters or more over
an HTML or plain-text page, served with success, holding fewer than 400 characters of text. It
is recorded as `answer_without_page_text` and nobody is told. It was a note once. Over 1,407
past answers it fired on 35, and on none was the answer at fault: on 22 the second fetch had
met a wall the fetch tool did not, on 8 the answer itself said the page was empty, and 5 were
the fetch tool's notice of a redirect. `retrieved status` counts it on a line of its own.

**A wall served with success** is the case a status cannot show: a check on the browser, a
request the server rejected, a demand for cookies or a sign-in page, each answered `200`. A page
holding under 2,000 characters of text that says one of a fixed list of phrases, such as
`checking your browser` or `request rejected`, is recorded as one, and so is a response whose
`cf-mitigated` header says the server challenged it. The record carries `wall`, the check is
`not_run` with `the independent fetch met a check on the browser (the page says "checking your
browser"), which is not the page the tool read`, and `retrieved status` counts the records.
Nothing is retried and the request is never dressed as a browser's. Over 1,082 stored pages
served with success, this named 70, each a wall on reading, and none of the 36 other pages
holding under 400 characters, which were unfilled shells, stubs, empty documents and error
pages served with success. A wall that says none of the phrases is still a short page, and is
`not_run` for that.

**A URL the rate limiter declines** is not fetched, and the answer is still there. Where the
library already holds a copy of that URL, the answer is checked against the most recent copy
with no request made. The outcome is one line of `answer_checks.jsonl`, with the digest of the
copy, when it was fetched and why no request was made. The copy's own record is not touched: it
is the account of the earlier fetch and keeps the check of the earlier answer. A note raised this
way says so:

```text
  retrieved: 1 passage the fetch answer quoted from https://example.com/docs is not in the copy
  of the page this library holds, fetched 2026-10-13T15:02:11+00:00: "...". Read the page before
  quoting it. The page was not fetched again for this check: the copy is from
  2026-10-13T15:02:11+00:00, and it can differ from what the fetch tool read.
```

Where the library holds no copy, the line in `skipped.jsonl` is the whole record and nothing is
checked. `retrieved status` counts these checks on a line of their own.

The same file takes one other case. A record is named by its bytes, so a page fetched again
unchanged names the record it already has. That record keeps the first answer's check, and the
later answer's check is a line of `answer_checks.jsonl` with `same_bytes_fetched_at` set. Its
note, if it raises one, is the ordinary one: the page was fetched.

A passage not found is appended to `flags.jsonl`, and nothing else is. The second entry `retrieved hook` prints,
`retrieved-hook --report`, hands it to the agent once, on the next tool call that session makes,
and prints nothing on every other call. Without that entry the check still runs and is recorded;
`retrieved status` counts what was flagged.

**Subagents.** A subagent's fetch runs the same hooks, with the session's `session_id` and an
`agent_id` of its own. A flag records that `agent_id`. A subagent is told about flags its own
fetches raised and no others; the main agent is told about its own and any subagent's, and a
note about a subagent's fetch ends `The fetch was made by a subagent of this session.` Whichever
of the two calls a tool first is told. A subagent whose fetch was its last tool call has
finished before the check has, so the main agent is the only reader left for that flag.

What this does not catch is most of what can go wrong. A paraphrase that changes the meaning has
no quotation marks around it, and neither does anything else the answer says in its own words. A
quotation the page does hold can still be quoted against its sense. And the page is this library's
retrieval, so a passage reported missing may have been on the page the fetch tool read.

Single quotation marks are not read. The apostrophe is the same character, so `the page's 'first'
and the keeper's` pairs wherever the next one falls, and a passage quoted that way is not checked.
A quotation whose words are also in the prompt is not checked either, including one the answer
attributes to the page.

What it gets wrong, in the other direction, is a passage the answer reproduced faithfully from a
page that writes it another way. A formula is the common case: an arXiv listing or feed carries
`$\rho = 0.19$` as TeX, the answer writes `ρ = 0.19`, and the passage is reported missing. Text a
page hides from sight and keeps in its markup (`opens in new tab`) is another. An answer that
drops a citation or a parenthesis from the middle of a quotation without marking the cut is
reported too, and there the report is right: those words in that order are not on the page.

## What it refuses to fetch

Re-fetching a URL an agent chose is a server-side request forgery primitive: the agent names the
target and this library makes the request, from a machine that may hold credentials. The denylist
is the security boundary, and it is checked on the URL requested, on the URL redirects actually
reached, and on wherever a browser navigated if one rendered the page.

| refused | because |
|---|---|
| `169.254.169.254` and other metadata endpoints | a re-fetch writes cloud credentials to disk as evidence |
| `localhost`, RFC 1918, link-local, `.internal` | the agent's network is not the public web |
| `file:`, `data:`, extension schemes | not retrievals in any sense worth recording |
| URLs with a token, credential or identifier in the query | the record outlives the secret |

`Set-Cookie` and `Authorization` are dropped before a record is written. Storing headers for a
future WARC export is worth doing; storing them naively makes an evidence store a secrets store.

## How often it asks

Capturing only what an agent already read keeps this near one extra request per human-initiated
fetch, which is browser-shaped rather than crawler-shaped. That ratio is not automatic — an agent
researching a topic pulls thirty pages from one host in a minute — so the limits are enforced
rather than assumed.

| limit | value |
|---|---|
| gap between two requests to one host | 2s, or the host's `Crawl-delay` where it is longer |
| a URL fetched again within | 15 minutes is not fetched again |
| captures per session | 100 |
| `robots.txt` | honored, cached for a day, and fetched through the denylist |

The command is paced the same way the hook is. A rate limit one entry point honors and another
ignores is not a rate limit, and a script in a loop uses the entry point that ignores it. Pass
`--now` to capture something the limit would have held.

Every refusal lands in `skipped.jsonl` with its reason, so a missing capture can be told apart
from a page nobody fetched.

## Pages built by JavaScript

An HTTP client sees the shell a single-page app serves: a few hundred bytes, a script tag, no
text. Those bytes really were served, so storing them is not wrong -- but a text digest over an
empty page answers nothing.

```bash
pip install "retrieved[browser]"
python -m playwright install chromium
```

With a browser installed, a page that looks like a shell is rendered and the record says
`javascript_executed: true` beside the engine and version. The browser navigates for itself, so
where it landed is checked against the denylist too and recorded as the record's `final_url`.

Without a browser nothing changes and the record says `javascript_executed: false`, which is
true. The extra is optional because a headless browser is a few hundred megabytes and a second
per page, and a tool that demands one is a tool most people will not install.

## What it does not do

- **Judge whether a page supports a claim.** That is a semantic question and this is a
  deterministic tool. It answers *do these bytes contain this passage*, nothing more.
- **Crawl.** It fetches only URLs an agent already read — roughly one extra request per
  human-initiated fetch. Search results are not fetched: those are links nobody read.
- **Re-fetch on a timer.** Scheduled re-verification would turn a 1:1 request ratio into
  unbounded volume, which is the line between a verifier and a crawler.
- **Republish.** Captures stay local. Deleting content on request removes the bytes and keeps the
  record, so the fact of retrieval survives the removal of what was retrieved.

## Related

`retrieved` stores what was fetched. [`reproducible-science`](https://github.com/elliottower/reproducible-science)
checks that a manuscript's numbers and quotations still match the artifacts behind them, across
preregistration, runs, and sources. This depends on nothing in that toolkit; that toolkit depends
on nothing here.

## License

MIT.
