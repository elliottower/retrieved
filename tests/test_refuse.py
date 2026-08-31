import pytest

from retrieved.refuse import Refused, check, check_final


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.200.200/latest/meta-data/",
    ],
)
def test_metadata_endpoints_are_refused(url):
    """The worst outcome available: a re-fetch writing cloud credentials to disk as evidence."""
    with pytest.raises(Refused):
        check(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/admin",
        "http://127.0.0.1/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://printer.local/status",
        "http://wiki.internal/secrets",
    ],
)
def test_private_networks_are_refused(url):
    with pytest.raises(Refused):
        check(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<script>",
        "chrome-extension://abc/page.html",
        "ftp://x/y",
    ],
)
def test_non_web_schemes_are_refused(url):
    with pytest.raises(Refused):
        check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1/data?access_token=abc123",
        "https://example.com/doc?api_key=sk-live-xxxx",
        "https://example.com/reset?token=eyJhbGci",
        "https://example.com/p?email=someone@example.org",
        "https://example.com/chart?mrn=1234567",
    ],
)
def test_urls_carrying_a_secret_or_identifier_are_refused(url):
    """Refused rather than stored with the value stripped: a redacted URL is not the URL fetched."""
    with pytest.raises(Refused):
        check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://arxiv.org/abs/2211.00593",
        "https://docs.python.org/3/library/re.html?highlight=match",
        "https://example.com/search?q=how+does+this+work",
    ],
)
def test_ordinary_public_pages_are_allowed(url):
    check(url)


def test_a_redirect_into_a_private_network_is_refused_and_names_both_urls():
    """The check before the request cannot see where a shortener points. This is the one that
    matters, and it is the case a first version forgets."""
    with pytest.raises(Refused) as caught:
        check_final("https://bit.ly/xyz", "http://169.254.169.254/latest/meta-data/")
    assert "bit.ly" in str(caught.value)
    assert "metadata" in str(caught.value)


def test_the_refusal_carries_a_reason_for_the_record():
    with pytest.raises(Refused) as caught:
        check("http://169.254.169.254/")
    assert caught.value.reason
    assert caught.value.url == "http://169.254.169.254/"
