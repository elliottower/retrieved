# Changelog

## Unreleased

- The hook checks the fetch tool's answer against the page it retrieved. Passages the answer puts
  in quotation marks are looked for in the page's text, and the record gains an `answer_check`
  block saying they were all found, which were not, or why nothing was checked. The answer is
  not stored, only its digest.
- A long answer over a page with almost no text is recorded as `answer_without_page_text`,
  separately from quotations. It is recorded only: it writes no flag and produces no note, and
  `retrieved status` counts it on its own line. As a note it was wrong each of the 35 times it
  fired on past answers.
- A second fetch answered with an error status is `not_run`, and no passage is looked for in an
  error page. Where the fetch tool's own request succeeded, the reason says the page the tool
  read was not obtained.
- A later fetch that returns the same bytes no longer replaces the record and the earlier
  answer's check in it. The later check is appended to `answer_checks.jsonl`.
- A passage not found is appended to `flags.jsonl`, and `retrieved-hook --report`, registered for every
  tool, tells the agent once on the session's next tool call. It prints nothing otherwise.
  `retrieved hook` prints both entries.
- `retrieved status` reports how many answers were flagged, checked, not checked, and never
  recorded as either.
- A record without `answer_check` reads as it did: `verify`, `status`, `promotable` and `promote`
  treat the block as optional.
- A PDF is checked against the text `pdftotext` extracts from it, where that program is
  installed. Where it is not, where it fails or runs out of time, or where it finds no text, the
  check does not run and says which.
- JSON is checked against its string values, parsed, so an escaped character compares as the
  character. XML and Atom are checked with their tags removed.
- `answer_check` names what produced the text checked, under `checked_against`.
- A URL the rate limiter declines is checked against the most recent copy the library holds, with
  no request made. The outcome goes to `answer_checks.jsonl`, with which copy it was and when it
  was fetched, and a note raised from it says the page was not fetched again.
- A passage split on the page by a tag inside a word, by emphasis the answer wrote as markdown,
  by a nested quotation's marks or by a line-end hyphen in a PDF is found. A quotation that runs
  over a line end in the answer is read, and so is a markdown block quote.
- A quoted passage that is the prompt's own words is not looked for on the page, which is what
  the fetch tool's notice of a redirect consists of. Nor is an attribute's value in markup the
  answer reproduces.
- A flag records which subagent's fetch raised it. A subagent is told about its own flags, and
  the main agent about its own and any subagent's.
- `scripts/backup_to_gcs.sh` copies `flags.jsonl` and `answer_checks.jsonl`.

## 0.1.0

- `retrieved capture` fetches a URL independently, stores the bytes, and records two digests: one
  over the bytes, one over the extracted text with the extractor named beside it.
- A `PostToolUse` hook captures pages an agent fetches, as a byproduct of ordinary use.
- A denylist that refuses cloud metadata endpoints, private networks, non-HTTP schemes and URLs
  carrying credentials, checked before the request and again after redirects.
- `retrieved verify` re-hashes stored bytes against their names. `retrieved status` reports what
  drifted, measured on the text digest.
- `retrieved history <url>` lists every capture of one URL and marks the fetches where the
  reading changed.
- Rate limiting applies to the command as well as the hook, so a script calling `retrieved
  capture` in a loop is paced like anything else. `--now` overrides it for a single capture.
- The denylist is checked before the rate limiter rather than after. In the other order a session
  working against private hosts spent its capture budget on requests that were never made, and
  the limiter's own `robots.txt` fetch reached those hosts before any check ran.
- `robots.txt` is fetched through the denylist.
- `Library.connect()` closes the handle when its block ends. `with sqlite3.connect(...)` commits
  and does not close.
- A rendered page is checked against the denylist a third time, against wherever the browser
  navigated. A page moving itself with `location =` after the response completes is a redirect
  the HTTP client's chain never sees.
- Passing a client to `fetch` keeps every request inside that transport, which means no browser.
  A render is a request this library makes on its own and an injected client cannot mediate it.
- The test suite refuses to open a connection, so a path that reaches the network is an error
  rather than a slow test.
