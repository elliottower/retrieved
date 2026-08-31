# Releasing

Single package, so no lockstep to keep. What remains is the part that cannot be undone.

## Before the tag

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv build && uv run twine check dist/*
```

The CI job `denylist-can-fail` must be green on the commit being released. It removes the
denylist and requires the suite to go red; if it passes with the guard removed, the security
boundary is untested and the release stops there.

## The tag

```bash
git tag -a vX.Y.Z -m "Release X.Y.Z"
git show --no-patch --decorate vX.Y.Z     # confirm it points at the audited commit
git push origin vX.Y.Z
```

## The GitHub Release, which archiving needs

Pushing a tag publishes to PyPI and archives nothing. Zenodo and similar watch for the Release
event, so a tag that was published and never released is invisible to them.

```bash
gh release create vX.Y.Z --title vX.Y.Z --notes "..."
```

Release the tag, never the branch: `gh release create` defaults to the current commit, so a
release cut from a later `main` archives code the published package does not contain.

## Why the version matters more here than usual

A PyPI version is consumed the moment it uploads and can never be reused, so a partially
published release is unrecoverable — it needs a new number. Check the version is absent from PyPI
before tagging, and check the trusted-publisher binding first: a rejected upload costs nothing, a
consumed version costs the number.
