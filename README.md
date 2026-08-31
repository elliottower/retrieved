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
author. The hook never blocks a fetch — `PostToolUse` cannot — and never prints, because a hook
that speaks on every fetch gets uninstalled. Refusals land in `skipped.jsonl`.

## What it refuses to fetch

Re-fetching a URL an agent chose is a server-side request forgery primitive: the agent names the
target and this library makes the request, from a machine that may hold credentials. The denylist
is the security boundary, and it is checked twice — once on the URL requested, once on the URL
redirects actually reached.

| refused | because |
|---|---|
| `169.254.169.254` and other metadata endpoints | a re-fetch writes cloud credentials to disk as evidence |
| `localhost`, RFC 1918, link-local, `.internal` | the agent's network is not the public web |
| `file:`, `data:`, extension schemes | not retrievals in any sense worth recording |
| URLs with a token, credential or identifier in the query | the record outlives the secret |

`Set-Cookie` and `Authorization` are dropped before a record is written. Storing headers for a
future WARC export is worth doing; storing them naively makes an evidence store a secrets store.

## Pages built by JavaScript

An HTTP client sees the shell a single-page app serves: a few hundred bytes, a script tag, no
text. Those bytes really were served, so storing them is not wrong -- but a text digest over an
empty page answers nothing.

```bash
pip install "retrieved[browser]"
python -m playwright install chromium
```

With a browser installed, a page that looks like a shell is rendered and the record says
`javascript_executed: true` beside the engine and version. Without one, nothing changes and the
record says `javascript_executed: false`, which is true. The extra is optional because a headless
browser is a few hundred megabytes and a second per page, and a tool that demands one is a tool
most people will not install.

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
