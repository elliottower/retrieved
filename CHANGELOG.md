# Changelog

## Unreleased

Initial work. Nothing published.

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
