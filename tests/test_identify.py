import pytest

from retrieved.identify import identify, slug_for


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://arxiv.org/abs/2211.00593", "2211.00593"),
        ("https://arxiv.org/html/2310.02207v3", "2310.02207"),
        ("https://arxiv.org/pdf/2408.01416", "2408.01416"),
    ],
)
def test_an_arxiv_id_is_read_from_the_url(url, expected):
    """The URL cannot be wrong about itself, where a meta tag is whatever the page claims."""
    assert identify(url)["arxiv"] == expected


def test_a_doi_is_read_from_a_citation_meta_tag():
    body = b'<meta name="citation_doi" content="10.1038/s41586-021-03819-2">'
    assert identify("https://example.com/paper", body)["doi"] == "10.1038/s41586-021-03819-2"


def test_an_arxiv_doi_becomes_an_arxiv_id_rather_than_both():
    """Recording both would make one work look like two, which is the duplicate defect itself."""
    body = b'<meta name="citation_doi" content="10.48550/arXiv.2502.04878">'
    found = identify("https://example.com/p", body)
    assert found == {"arxiv": "2502.04878"}


def test_an_ordinary_page_identifies_nothing():
    assert identify("https://example.com/", b"<html><body>hello</body></html>") == {}
    assert identify("https://httpbin.org/html") == {}


def test_a_page_that_identifies_nothing_gets_no_slug():
    """The whole point: an unidentifiable work stays a retrieval rather than becoming a record
    keyed by a hash of its title."""
    assert slug_for({}) == ""


@pytest.mark.parametrize(
    "identifiers,expected",
    [
        ({"doi": "10.1/X"}, "doi-10-1-x"),
        ({"arxiv": "2211.00593"}, "arxiv-2211-00593"),
        ({"doi": "10.1/x", "arxiv": "2211.00593"}, "doi-10-1-x"),
    ],
)
def test_the_slug_matches_what_a_citation_library_would_use(identifiers, expected):
    assert slug_for(identifiers) == expected


def test_the_url_wins_over_a_disagreeing_meta_tag():
    body = b'<meta name="citation_arxiv_id" content="9999.99999">'
    assert identify("https://arxiv.org/abs/2211.00593", body)["arxiv"] == "2211.00593"
