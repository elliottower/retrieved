import hashlib

import httpx
import pytest

from retrieved.capture import SECRET_HEADERS, fetch, normalize
from retrieved.refuse import Refused


def client_returning(*, body=b"<html><body>hello</body></html>", headers=None, status=200):
    """An httpx client answering from memory, so these tests make no network request."""

    def handler(request):
        return httpx.Response(
            status,
            content=body,
            headers=headers or {"content-type": "text/html"},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_both_digests_are_recorded_and_differ_in_what_they_cover():
    body = b"<html><head><style>p{color:red}</style></head><body>the claim</body></html>"
    with client_returning(body=body) as c:
        r = fetch("https://example.com/", client=c)
    assert r.bytes_sha256 == hashlib.sha256(body).hexdigest()
    assert r.text_sha256 == hashlib.sha256(b"the claim").hexdigest()
    assert r.bytes_sha256 != r.text_sha256


def test_markup_that_changes_every_fetch_does_not_change_the_text_digest():
    """The reason two digests exist. A byte digest answers 'same bytes' and always says no."""
    first = b'<html><body>the claim<input name="csrf" value="a1b2"></body></html>'
    second = b'<html><body>the claim<input name="csrf" value="z9y8"></body></html>'
    with client_returning(body=first) as c:
        a = fetch("https://example.com/", client=c)
    with client_returning(body=second) as c:
        b = fetch("https://example.com/", client=c)
    assert a.bytes_sha256 != b.bytes_sha256
    assert a.text_sha256 == b.text_sha256


@pytest.mark.parametrize("header", sorted(SECRET_HEADERS))
def test_credential_headers_never_reach_the_record(header):
    """Storing headers for a future WARC export is right; storing these makes it a secrets store."""
    with client_returning(headers={"content-type": "text/html", header: "sensitive-value"}) as c:
        r = fetch("https://example.com/", client=c)
    assert header not in r.response_headers
    assert "sensitive-value" not in str(r.response_headers)


def test_an_ordinary_header_is_kept():
    with client_returning(headers={"content-type": "text/html", "etag": 'W/"abc"'}) as c:
        r = fetch("https://example.com/", client=c)
    assert r.response_headers["etag"] == 'W/"abc"'


def test_a_refused_url_is_never_requested():
    def explode(request):  # pragma: no cover - reached only on failure
        raise AssertionError(f"a request was made to {request.url}")

    with httpx.Client(transport=httpx.MockTransport(explode)) as c, pytest.raises(Refused):
        fetch("http://169.254.169.254/latest/meta-data/", client=c)


def test_the_record_states_that_no_javascript_ran():
    """A JavaScript shell and an empty page read identically without this."""
    with client_returning() as c:
        r = fetch("https://example.com/", client=c)
    assert r.javascript_executed is False
    assert "no browser" in r.rendering_method


def test_normalize_leaves_non_html_alone_apart_from_whitespace():
    assert normalize(b"a\n\n  b", "text/plain") == "a b"
    assert normalize(b"<p>a</p>", "text/plain") == "<p>a</p>"


def test_an_injected_client_is_the_only_transport_used(monkeypatch):
    """A browser is a second request this library makes on its own, through neither the given
    transport nor its proxy or trust settings. With playwright installed, every test here that
    passes a mock client was reaching the live internet behind it."""

    def forbidden(url, **kwargs):
        raise AssertionError(f"a browser was launched for {url}")

    monkeypatch.setattr("retrieved.capture.render.render", forbidden)
    shell = b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>'
    with client_returning(body=shell) as c:
        r = fetch("https://example.com/", client=c)
    assert r.javascript_executed is False
    assert r.rendering_method == "httpx, no browser"


def test_a_shell_is_rendered_when_nothing_constrains_the_transport(monkeypatch):
    """The other half: with no client injected, a page that looks like a shell does get one."""
    real = httpx.Client

    def offline(*args, **kwargs):
        shell = b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>'
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=shell, headers={"content-type": "text/html"}, request=request
            )
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", offline)
    monkeypatch.setattr(
        "retrieved.capture.render.render",
        lambda url, timeout=20.0: (
            b"<html><body>the page after scripts</body></html>",
            "fake",
            url,
        ),
    )
    r = fetch("https://example.com/")
    assert r.javascript_executed is True
    assert r.rendering_method == "fake"
    assert normalize(r.body, "text/html") == "the page after scripts"


def test_a_page_the_browser_moves_to_a_private_host_is_refused(monkeypatch):
    """A page can move itself with `location =` after the HTTP response completes, which is a
    redirect the client's chain never saw. The bytes came from wherever the browser ended up."""
    real = httpx.Client

    def offline(*args, **kwargs):
        shell = b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>'
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=shell, headers={"content-type": "text/html"}, request=request
            )
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", offline)
    monkeypatch.setattr(
        "retrieved.capture.render.render",
        lambda url, timeout=20.0: (b"<html>internal</html>", "fake", "http://169.254.169.254/"),
    )
    with pytest.raises(Refused):
        fetch("https://example.com/")
