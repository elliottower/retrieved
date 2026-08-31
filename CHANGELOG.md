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
