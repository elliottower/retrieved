import hashlib

import httpx
import pytest

from retrieved import answer
from retrieved.answer import FOUND, NOT_FOUND, NOT_RUN, check, fold, occurs, quotations
from retrieved.capture import MAX_BYTES, fetch

# Written for these tests. Long enough to be a page that came with its content, and carrying the
# typography a real page does: a curly quotation, an en dash, an entity, a soft hyphen, a link
# that ends just before a comma.
PAGE = (
    "<html><head><style>p{margin:0}</style></head><body>"
    "<p>The lighthouse at Marrow Point was built in 1871 and automated a century later, "
    "after the last keeper retired to the mainland with his two dogs.</p>"
    "<p>Its lamp is visible for nineteen nautical miles in clear weather &amp; rather less "
    "in fog, which settles on the point for 40–60 days of every year.</p>"
    "<p>The harbour board called it “the most stubborn light on the northern coast” in its "
    "report for 1902, and nobody has disagreed since.</p>"
    "<p>Visitors may climb the tower between May and September, when the stair­case is "
    "dry and the <a href='/birds'>nesting season</a>, which closes the gallery, has ended.</p>"
    "</body></html>"
).encode()


def a_page(body=PAGE, ctype="text/html; charset=utf-8", status=200):
    def handler(request):
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        return fetch("https://example.com/lighthouse", client=c)


def test_a_quotation_that_is_on_the_page_is_found():
    said = 'The page states that the "lamp is visible for nineteen nautical miles" at best.'
    outcome = check(said, a_page())
    assert outcome.status == FOUND
    assert (outcome.quotations, outcome.found, outcome.not_found) == (1, 1, ())
    assert not outcome.flagged


@pytest.mark.parametrize("closing", [".", ",", ";", ":"])
def test_a_closing_mark_the_page_does_not_have_is_not_a_mismatch(closing):
    said = f'It says "the last keeper retired to the mainland{closing}" Then it goes on.'
    assert check(said, a_page()).status == FOUND


def test_one_changed_word_is_not_found_and_is_reported_verbatim():
    said = 'It says "the first keeper retired to the mainland with his two dogs" in passing.'
    outcome = check(said, a_page())
    assert outcome.status == NOT_FOUND
    assert outcome.not_found == ("the first keeper retired to the mainland with his two dogs",)
    assert (outcome.quotations, outcome.found) == (1, 0)
    assert outcome.flagged


def test_found_and_missing_passages_are_counted_separately():
    said = (
        'It was "built in 1871 and automated a century later" and, the page adds, '
        '"painted red and white in the spring of 1950" as well.'
    )
    outcome = check(said, a_page())
    assert outcome.status == NOT_FOUND
    assert (outcome.quotations, outcome.found) == (2, 1)
    assert outcome.not_found == ("painted red and white in the spring of 1950",)


@pytest.mark.parametrize(
    "said",
    [
        # straight marks in the answer, curly on the page
        'The board called it "the most stubborn light on the northern coast" in 1902.',
        # curly marks around the answer's quotation
        "It was “built in 1871 and automated a century later”, the page says.",
        # a hyphen where the page has an en dash
        'Fog "settles on the point for 40-60 days of every year" there.',
        # an ampersand the page writes as an entity
        'Visible "in clear weather & rather less in fog" by its account.',
        # a word the page breaks with a soft hyphen
        'Open "when the staircase is dry and the nesting season" is over.',
        # a comma the page puts after a closing tag
        'Open once "the nesting season, which closes the gallery, has ended" each year.',
        # case
        'It reads "THE LIGHTHOUSE AT MARROW POINT WAS BUILT IN 1871" at the top.',
    ],
)
def test_typography_that_changes_no_word_is_not_a_mismatch(said):
    outcome = check(said, a_page())
    assert (outcome.status, outcome.quotations) == (FOUND, 1)


@pytest.mark.parametrize(
    "passage",
    [
        "The lighthouse at Marrow Point ... was automated a century later",
        "The lighthouse at Marrow Point … was automated a century later",
        "The lighthouse [at Marrow Point] was built in 1871 and automated",
    ],
)
def test_a_passage_marked_as_edited_is_not_extracted(passage):
    assert quotations(f'The page says "{passage}" about it.') == []
    assert quotations(f"The page says “{passage}” about it.") == []


def test_the_text_between_two_quotations_is_never_a_third():
    said = (
        'The page calls it "stubborn" and then, moving on to the matter of opening hours, '
        '"closed" for the winter.'
    )
    assert quotations(said) == []

    said = (
        'It was "built in 1871 and automated a century later" and, to give the other passage '
        'in full as well, "the last keeper retired to the mainland with his two dogs" too.'
    )
    assert quotations(said) == [
        "built in 1871 and automated a century later",
        "the last keeper retired to the mainland with his two dogs",
    ]


def test_a_quotation_broken_across_lines_does_not_lend_its_closing_mark_to_the_next():
    said = (
        'The page says "the lamp is visible for nineteen\nnautical miles." and after that it '
        'turns to the question of visiting "hours".'
    )
    assert quotations(said) == []


def test_an_inch_mark_does_not_open_a_quotation():
    said = 'The lens is 36" across and, according to the page, weighs rather more than "a ton".'
    assert quotations(said) == []


@pytest.mark.parametrize(
    "quoted",
    [
        "https://example.com/lighthouse/visiting-hours-and-tides",
        "marrow_point_lighthouse_visiting_hours.pdf",
    ],
)
def test_a_quoted_string_with_no_space_is_not_a_quotation(quoted):
    assert quotations(f'See "{quoted}" for the timetable.') == []


def test_a_short_quoted_term_is_not_a_quotation():
    assert len("the most stubborn light") < answer.MIN_QUOTATION_CHARS
    assert quotations('The board called it "the most stubborn light" once.') == []


def test_a_passage_quoted_twice_is_checked_once():
    said = 'It was "built in 1871 and automated a century later". Again: "built in 1871 and '
    said += 'automated a century later".'
    assert quotations(said) == ["built in 1871 and automated a century later"]


def test_folding_keeps_the_words_and_drops_the_typesetting():
    assert fold("  “Café&nbsp;Noir” — open­ing  ,  now  ") == '"café noir" - opening, now'
    assert not occurs("an advanced tool for the trade", fold("this advanced tool for the trade"))
    assert not occurs(" .,;: ", fold("anything at all"))


@pytest.mark.parametrize(
    "ctype,body",
    [
        ("application/pdf", b"%PDF-1.7 the last keeper retired to the mainland " * 20),
        ("application/octet-stream", bytes(range(256)) * 4),
        ("application/json", b'{"text": "the last keeper retired to the mainland"}' * 20),
        ("", b"the last keeper retired to the mainland with his two dogs " * 20),
    ],
)
def test_a_page_that_is_not_extracted_text_is_not_run_and_never_found(ctype, body):
    said = 'It says "the last keeper retired to the mainland with his two dogs" in passing.'
    outcome = check(said, a_page(body=body, ctype=ctype))
    assert outcome.status == NOT_RUN
    assert "not text this library extracts" in outcome.reason
    assert outcome.page_text_chars is None
    assert (outcome.quotations, outcome.found, outcome.not_found) == (0, 0, ())
    assert not outcome.flagged


def test_plain_text_is_checked_without_being_treated_as_markup():
    body = b"The lighthouse at Marrow Point was built in 1871 and automated a century later. " * 8
    said = 'It was "built in 1871 and automated a century later" there.'
    assert check(said, a_page(body=body, ctype="text/plain")).status == FOUND


@pytest.mark.parametrize("nothing", [None, "", "   \n", 0, ["a list"], {"result": "nested"}])
def test_no_answer_is_not_run_with_that_as_the_reason(nothing):
    outcome = check(nothing, a_page())
    assert outcome.status == NOT_RUN
    assert outcome.reason == "no answer in the payload"
    assert outcome.answer_sha256 == ""
    assert not outcome.flagged


def test_a_truncated_page_is_not_run_because_a_passage_past_the_cut_would_read_as_missing():
    body = b"<html><body>" + b"filler text that says nothing. " * (MAX_BYTES // 31 + 1)
    page = a_page(body=body + b"the passage at the very end of an enormous page</body></html>")
    assert page.truncated
    outcome = check('It ends "the passage at the very end of an enormous page" there.', page)
    assert outcome.status == NOT_RUN
    assert "truncated" in outcome.reason
    assert not outcome.flagged


def test_an_answer_that_quotes_nothing_is_not_run_rather_than_found():
    outcome = check("The lighthouse was built in 1871 and is open in summer.", a_page())
    assert outcome.status == NOT_RUN
    assert "quotes nothing" in outcome.reason
    assert not outcome.flagged


WALL = b"<html><body><h1>Sign in</h1><p>Please sign in to continue reading.</p></body></html>"


def test_a_long_answer_over_a_page_with_almost_no_text_raises_its_own_flag():
    said = "The lighthouse was built in 1871. " * 30
    assert len(said) >= answer.LONG_ANSWER_CHARS
    outcome = check(said, a_page(body=WALL, status=403))
    assert outcome.answer_without_page_text
    assert outcome.flagged
    # Its own flag: no quotation was checked, so none is reported as failing.
    assert outcome.status == NOT_RUN
    assert "too few to check against" in outcome.reason
    assert outcome.not_found == ()
    assert outcome.answer_chars == len(said)
    assert outcome.page_text_chars == len("Sign in Please sign in to continue reading.")


def test_quotations_are_not_checked_against_a_page_with_almost_no_text():
    said = 'It says "the last keeper retired to the mainland with his two dogs" in passing.'
    outcome = check(said, a_page(body=WALL))
    assert outcome.status == NOT_RUN
    assert outcome.not_found == ()
    assert not outcome.answer_without_page_text
    assert not outcome.flagged


def test_a_short_answer_over_a_short_page_is_the_fetch_tool_working():
    outcome = check("The page asks the reader to sign in.", a_page(body=WALL))
    assert not outcome.answer_without_page_text
    assert not outcome.flagged


def test_a_long_answer_over_a_page_with_its_content_raises_nothing():
    outcome = check("The lighthouse was built in 1871. " * 30, a_page())
    assert not outcome.answer_without_page_text
    assert not outcome.flagged


def test_the_flag_turns_on_exactly_at_the_named_thresholds():
    sparse = b"<html><body>" + b"x" * (answer.SPARSE_PAGE_CHARS - 1) + b"</body></html>"
    enough = b"<html><body>" + b"x" * answer.SPARSE_PAGE_CHARS + b"</body></html>"
    long = "y" * answer.LONG_ANSWER_CHARS
    assert check(long, a_page(body=sparse)).answer_without_page_text
    assert not check(long[:-1], a_page(body=sparse)).answer_without_page_text
    assert not check(long, a_page(body=enough)).answer_without_page_text


def test_the_record_block_holds_the_digest_of_the_answer_and_never_the_answer():
    said = 'It says "the first keeper retired to the mainland with his two dogs" in passing.'
    block = check(said, a_page()).as_record()
    assert block["answer_sha256"] == hashlib.sha256(said.encode()).hexdigest()
    assert block["answer_chars"] == len(said)
    assert said not in str(block)
    assert block["not_found"] == ["the first keeper retired to the mainland with his two dogs"]


def test_a_passage_not_found_is_capped_in_the_record():
    passage = "a sentence the page never contained, " * 20
    block = check(f'The page says "{passage.strip()}" somewhere.', a_page()).as_record()
    assert len(block["not_found"]) == 1
    assert block["not_found"][0] == passage[: answer.PASSAGE_CAP]


def test_half_a_surrogate_pair_in_the_answer_cannot_raise():
    said = 'It says "the last keeper retired \ud83d to the mainland" in passing.'
    outcome = check(said, a_page())
    assert outcome.status == NOT_FOUND
    assert outcome.as_record()["not_found"][0].encode()
