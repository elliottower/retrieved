import pytest

from retrieved import render
from retrieved.capture import normalize

SHELL = (
    b"<html><head><title>App</title></head>"
    b'<body><div id="root"></div><script src="/a.js"></script></body></html>'
)
REAL = (
    b"<html><body><article>"
    + b"This page came with its content. " * 20
    + b"</article></body></html>"
)


def test_a_javascript_shell_is_recognised():
    text = normalize(SHELL, "text/html")
    assert render.looks_like_a_shell(SHELL, text, "text/html")


def test_a_page_that_came_with_its_content_is_not_rendered():
    text = normalize(REAL, "text/html")
    assert not render.looks_like_a_shell(REAL, text, "text/html")


@pytest.mark.parametrize("ctype", ["application/pdf", "text/plain", "application/json"])
def test_only_html_is_ever_a_candidate(ctype):
    """A PDF with little extractable text needs a different tool, not a browser."""
    assert not render.looks_like_a_shell(SHELL, "", ctype)


def test_a_short_but_complete_page_is_left_alone():
    """Wrong in the cheap direction: rendering a page that did not need it costs a second;
    skipping one that did costs a capture that says nothing. But a real short page is common."""
    body = b"<html><body><p>" + b"x" * 500 + b"</p></body></html>"
    assert not render.looks_like_a_shell(body, normalize(body, "text/html"), "text/html")


def test_render_returns_none_when_no_browser_is_installed():
    """The whole extra is optional. Absent a browser this degrades to the plain fetch rather
    than failing, and every record then honestly says javascript_executed: false."""
    if render.available():
        pytest.skip("a browser is installed here, so the unavailable path cannot be exercised")
    assert render.render("https://example.com/") is None


def test_available_reports_a_state_rather_than_raising():
    assert isinstance(render.available(), bool)
