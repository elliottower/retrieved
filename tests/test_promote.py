import httpx
import pytest
import yaml

from retrieved.capture import fetch
from retrieved.promote import CannotPromote, candidates, promote
from retrieved.store import Library


def capture_into(library, url, body=b"<html><body>a paper</body></html>", ctype="text/html"):
    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": ctype}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = fetch(url, client=c)
    library.write(r)
    return r.bytes_sha256


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / ".retrieved").create()


def test_an_identified_work_promotes_with_its_bytes(library, tmp_path):
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    home = tmp_path / "citations"
    path = promote(library, digest, home)

    record = yaml.safe_load(path.read_text())
    assert record["slug"] == "arxiv-2211-00593"
    assert record["arxiv"] == "2211.00593"
    assert record["sha256"] == digest
    # The artifact travels with the record: a library of links is what content addressing replaces.
    assert (home / record["local"]).read_bytes() == (library.store / digest).read_bytes()


def test_a_page_with_no_identifier_refuses_to_become_a_record(library, tmp_path):
    """The whole design: an unidentifiable capture stays a retrieval rather than being filed
    under a title hash, where it duplicates the moment anything cites the work properly."""
    digest = capture_into(library, "https://example.com/some-blog-post")
    with pytest.raises(CannotPromote) as caught:
        promote(library, digest, tmp_path / "citations")
    assert "no DOI or arXiv id" in str(caught.value)


def test_promotion_never_happens_by_capturing(library, tmp_path):
    """Capture is automatic; promotion is a decision. Writing a capture must not touch a library."""
    home = tmp_path / "citations"
    capture_into(library, "https://arxiv.org/abs/2211.00593")
    assert not home.exists()


def test_an_existing_record_is_not_overwritten_without_force(library, tmp_path):
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    home = tmp_path / "citations"
    promote(library, digest, home)
    with pytest.raises(CannotPromote) as caught:
        promote(library, digest, home)
    assert "already in" in str(caught.value)
    promote(library, digest, home, force=True)


def test_authors_and_year_are_left_blank_rather_than_guessed(library, tmp_path):
    """A wrong author list beside a real identifier is the failure `citations audit` exists for."""
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    record = yaml.safe_load(promote(library, digest, tmp_path / "citations").read_text())
    assert record["authors"] == []
    assert record["year"] == ""
    assert "citations resolve" in record["note"]


def test_a_retrieval_whose_bytes_were_deleted_cannot_be_promoted(library, tmp_path):
    """A takedown removes bytes and keeps the record. Promoting one would produce a record whose
    artifact does not exist, which is worse than refusing."""
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    (library.store / digest).unlink()
    with pytest.raises(CannotPromote) as caught:
        promote(library, digest, tmp_path / "citations")
    assert "bytes for" in str(caught.value)


def test_candidates_lists_only_what_could_be_promoted(library):
    capture_into(library, "https://arxiv.org/abs/2211.00593")
    capture_into(library, "https://example.com/blog", body=b"<html>not a paper</html>")
    found = candidates(library)
    assert len(found) == 1
    assert found[0][1] == "arxiv-2211-00593"


def test_a_digest_prefix_is_enough(library, tmp_path):
    """`promotable` prints sixteen characters. A tool that then demands sixty-four disagrees
    with itself."""
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    path = promote(library, digest[:16], tmp_path / "citations")
    assert yaml.safe_load(path.read_text())["sha256"] == digest


def test_an_ambiguous_prefix_is_reported_rather_than_resolved(library, tmp_path):
    capture_into(library, "https://arxiv.org/abs/2211.00593")
    capture_into(library, "https://arxiv.org/abs/2310.02207", body=b"<html>another</html>")
    shared = [p.stem for p in library.retrievals.glob("*.yaml")]
    prefix = ""
    for i in range(1, 64):
        if len({s[:i] for s in shared}) < len(shared):
            prefix = shared[0][:i]
        else:
            break
    if prefix:  # only meaningful when two digests genuinely share a prefix
        with pytest.raises(CannotPromote) as caught:
            promote(library, prefix, tmp_path / "citations")
        assert "matches" in str(caught.value)


def test_one_damaged_record_does_not_end_the_survey(library, tmp_path):
    """A record is read back off disk, which is a boundary: the file may have been edited or
    truncated since it was written. Read as a plain dict, a missing key was a KeyError from
    whichever line touched it first, and one bad file ended `promotable` for the whole library."""
    good = capture_into(library, "https://arxiv.org/abs/2211.00593")
    damaged = capture_into(library, "https://arxiv.org/abs/1706.03762", body=b"<html>other</html>")
    (library.retrievals / f"{damaged}.yaml").write_text("fetched_at: 2026-01-01\n")

    listed = candidates(library)
    assert [row[0] for row in listed] == [good]


def test_promoting_a_damaged_record_says_what_is_wrong_with_it(library, tmp_path):
    """The survey tolerates it; asking about that one digest explains it."""
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    (library.retrievals / f"{digest}.yaml").write_text("fetched_at: 2026-01-01\n")
    with pytest.raises(CannotPromote) as caught:
        promote(library, digest, tmp_path / "citations")
    assert "not a readable record" in str(caught.value)
    assert "final_url" in str(caught.value)


def test_an_empty_record_file_is_reported_rather_than_crashing(library, tmp_path):
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    (library.retrievals / f"{digest}.yaml").write_text("")
    with pytest.raises(CannotPromote):
        promote(library, digest, tmp_path / "citations")


def test_a_field_a_later_version_added_is_ignored_rather_than_refused(library, tmp_path):
    """Forward compatibility in the direction that matters: a library written by a newer release
    stays readable here, because an unknown key is a new field and not a corrupt record."""
    digest = capture_into(library, "https://arxiv.org/abs/2211.00593")
    path = library.retrievals / f"{digest}.yaml"
    record = yaml.safe_load(path.read_text())
    record["some_field_from_2027"] = {"nested": True}
    path.write_text(yaml.safe_dump(record))
    assert promote(library, digest, tmp_path / "citations").is_file()
