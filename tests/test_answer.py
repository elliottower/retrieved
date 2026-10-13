import hashlib
import json
import shutil
import subprocess

import httpx
import pytest

from retrieved import answer, extract
from retrieved.answer import FOUND, NOT_FOUND, NOT_RUN, check, fold, occurs, quotations, squeeze
from retrieved.capture import MAX_BYTES, fetch, normalize

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


def test_a_quotation_that_runs_over_a_line_end_is_extracted_with_a_space_for_the_break():
    said = (
        'The page says its "lamp is visible for nineteen\nnautical miles in clear weather" and '
        'after that it turns to the question of visiting "hours".'
    )
    assert quotations(said) == ["lamp is visible for nineteen nautical miles in clear weather"]
    assert check(said, a_page()).status == FOUND


def test_a_quotation_does_not_run_over_a_blank_line():
    said = (
        'The gallery is "closed for the nesting season\n\n'
        "and the stair is dry between May and September\" by the page's account."
    )
    assert quotations(said) == []
    # One line end between the same marks is a quotation, so it is the blank line that ends it.
    assert quotations(said.replace("\n\n", "\n")) == [
        "closed for the nesting season and the stair is dry between May and September"
    ]


@pytest.mark.parametrize(
    "block",
    [
        "> The lighthouse at Marrow Point was built in 1871 and automated a century later",
        # the lines of one block are one passage
        "> The lighthouse at Marrow Point was built in 1871\n> and automated a century later",
        # emphasis around the block is the answer's
        "> *The lighthouse at Marrow Point was built in 1871 and automated a century later*",
        ">The lighthouse at Marrow Point was built in 1871 and automated a century later",
    ],
)
def test_a_markdown_block_quote_is_a_quotation(block):
    said = f"The page opens:\n\n{block}\n\nand then turns to the lamp."
    assert quotations(said) == [
        "The lighthouse at Marrow Point was built in 1871 and automated a century later"
    ]
    assert check(said, a_page()).status == FOUND


def test_a_block_quote_the_page_does_not_hold_is_not_found():
    said = "The page opens:\n\n> The lighthouse at Marrow Point was built in 1971 and automated\n"
    outcome = check(said, a_page())
    assert outcome.status == NOT_FOUND
    assert outcome.not_found == ("The lighthouse at Marrow Point was built in 1971 and automated",)


def test_two_block_quotes_are_two_passages_and_a_short_one_is_none():
    said = (
        "> The lighthouse at Marrow Point was built in 1871\n>\n"
        "> Its lamp is visible for nineteen nautical miles\n\n> Open in summer\n"
    )
    assert quotations(said) == [
        "The lighthouse at Marrow Point was built in 1871",
        "Its lamp is visible for nineteen nautical miles",
    ]


def test_a_block_quote_with_quotation_marks_in_it_is_left_to_the_marks():
    said = '> "The lighthouse at Marrow Point was built in 1871," the harbour board wrote in 1902.'
    assert quotations(said) == ["The lighthouse at Marrow Point was built in 1871,"]


@pytest.mark.parametrize(
    "block",
    [
        "> ## Visiting the lighthouse at Marrow Point\n> The tower is open from May to September",
        "> - the tower is open between May and September\n> - the gallery closes for nesting",
        "> | month | the tower is open or closed to visitors |",
    ],
)
def test_a_block_that_is_a_piece_of_a_document_is_not_a_quotation(block):
    assert quotations(f"The page, as served:\n\n{block}\n") == []


def test_a_quotation_in_marks_inside_a_block_does_not_carry_the_blocks_marks():
    said = '> The board wrote "the most stubborn light\n> on the northern coast" in its report.'
    assert quotations(said) == ["the most stubborn light on the northern coast"]


def test_single_quotation_marks_are_not_read():
    said = "The page says 'the last keeper retired to the mainland with his three dogs' there."
    assert quotations(said) == []


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="/plan.svg" alt="A plan of the harbour with the lighthouse at its mouth" />',
        '<a title="The harbour board and its report for 1902" href="/board">board</a>',
    ],
)
def test_an_attributes_value_is_not_a_quotation(markup):
    assert quotations(f"The page as served:\n\n{markup}\n") == []


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
    assert fold("the point -- which -- is \u2061windy\u200b") == "the point - which - is windy"
    assert not occurs("an advanced tool for the trade", fold("this advanced tool for the trade"))
    assert not occurs(" .,;: ", fold("anything at all"))


@pytest.mark.parametrize(
    "ctype,body",
    [
        ("application/octet-stream", bytes(range(256)) * 4),
        ("application/zip", b"PK\x03\x04 the last keeper retired to the mainland " * 20),
        ("", b"the last keeper retired to the mainland with his two dogs " * 20),
    ],
)
def test_a_page_that_is_not_extracted_text_is_not_run_and_never_found(ctype, body):
    said = 'It says "the last keeper retired to the mainland with his two dogs" in passing.'
    outcome = check(said, a_page(body=body, ctype=ctype))
    assert outcome.status == NOT_RUN
    assert "not text this library extracts" in outcome.reason
    assert outcome.page_text_chars is None
    assert outcome.checked_against == ""
    assert (outcome.quotations, outcome.found, outcome.not_found) == (0, 0, ())
    assert not outcome.flagged


def test_plain_text_is_checked_without_being_treated_as_markup():
    body = b"The lighthouse at Marrow Point was built in 1871 and automated a century later. " * 8
    said = 'It was "built in 1871 and automated a century later" there.'
    outcome = check(said, a_page(body=body, ctype="text/plain"))
    assert (outcome.status, outcome.checked_against) == (FOUND, "retrieved.normalize/raw")


def test_the_record_names_what_produced_the_text_an_html_page_was_checked_against():
    said = 'The page states that the "lamp is visible for nineteen nautical miles" at best.'
    assert check(said, a_page()).as_record()["checked_against"] == "retrieved.normalize/html"
    nothing_quoted = check("The lighthouse is open in summer.", a_page())
    assert nothing_quoted.as_record()["checked_against"] == "retrieved.normalize/html"


# Padding for the short synthetic pages below, so each is a page that came with its content.
FILLER = (
    "<p>The harbour office keeps the tide tables, the register of moorings and a ledger of "
    "wrecks going back to 1840, and opens on weekday mornings from October to March.</p>"
    "<p>Pilotage is compulsory for vessels over forty metres, and the pilot boards at the "
    "fairway buoy two miles north-east of the point, weather and swell permitting.</p>"
    "<p>The lifeboat station beside it was rebuilt in 1996 after the old slipway failed.</p>"
)


def a_short_page(fragment, ctype="text/html"):
    return a_page(body=f"<html><body>{FILLER}{fragment}</body></html>".encode(), ctype=ctype)


@pytest.mark.parametrize(
    "fragment,said",
    [
        # an inline tag in the middle of a word
        (
            "<p>The keeper<b>s</b> of the light drew their water from a cistern.</p>",
            'It says "the keepers of the light drew their water from a cistern" there.',
        ),
        # emphasis the fetch tool read as markdown, which the page writes as a tag
        (
            "<p>The cistern was <em>never</em> known to run dry in summer.</p>",
            'It says "the cistern was _never_ known to run dry in summer" there.',
        ),
        (
            "<p>The cistern was <b>never</b> known to run dry in summer.</p>",
            'It says "the cistern was **never** known to run dry in summer" there.',
        ),
        # a quotation nested in the answer's own, where the page has double marks
        (
            "<p>The board called the cistern “a triumph of masonry” in its report.</p>",
            "It says \"the board called the cistern 'a triumph of masonry' in its report\".",
        ),
        # the answer's full stop inside its nested closing mark, where the page has a comma
        (
            "<p>The board called the cistern “a triumph of masonry”, and paid for it.</p>",
            "It says \"the board called the cistern 'a triumph of masonry.'\" And more.",
        ),
        # a markdown escape
        (
            "<p>The rate was 4*2 pence a gallon to vessels in the roads.</p>",
            'It says "the rate was 4\\*2 pence a gallon to vessels in the roads" there.',
        ),
        # two hyphens for a dash, and a function name set off by an invisible character
        (
            "<p>The cistern--the second on the site--holds f\u2061(x) gallons of rainwater.</p>",
            'It says "the cistern—the second on the site—holds f(x) gallons of rainwater".',
        ),
    ],
)
def test_what_markup_leaves_inside_a_sentence_is_not_a_mismatch(fragment, said):
    outcome = check(said, a_short_page(fragment))
    assert (outcome.status, outcome.quotations, outcome.found) == (FOUND, 1, 1)


@pytest.mark.parametrize(
    "said",
    [
        # another word
        'It says "the keepers of the lamp drew their water from a cistern" there.',
        # a sign and a decimal point survive the squeeze
        'It says "the cistern lost -5 inches of water in the dry summer of 1921" there.',
        'It says "the cistern lost 15 inches of water in the dry summer of 1921" there.',
    ],
)
def test_the_looser_comparison_lets_no_other_word_or_number_through(said):
    fragment = (
        "<p>The keeper<b>s</b> of the light drew their water from a cistern.</p>"
        "<p>The cistern lost 5 inches of water in the dry summer of 1921, and 1.5 the next.</p>"
    )
    assert check(said, a_short_page(fragment)).status == NOT_FOUND


def test_squeezing_removes_marks_and_keeps_every_letter_digit_and_sign():
    assert squeeze(fold("The keeper s' “_water_” \\* -5 and 1.5")) == "thekeepers" + "water-5and1.5"
    # The known cost, which the minimum length is there to bound.
    assert squeeze(fold("a nice box")) == squeeze(fold("an icebox"))
    assert squeeze(fold("well-known"), typeset=True) == "wellknown"
    assert squeeze(fold("a change of -5 and 40-60"), typeset=True) == "achangeof-5and40-60"


def test_formula_source_beside_the_formula_is_not_a_mismatch():
    fragment = (
        "<p>The agreement was high (<math><mi>κ</mi><annotation encoding='application/x-tex'>"
        "\\kappa</annotation></math> = 0.82) for every pair of lighthouse inspectors.</p>"
    )
    said = 'It reports "the agreement was high (κ = 0.82) for every pair of lighthouse inspectors".'
    assert check(said, a_short_page(fragment)).status == FOUND


def test_tex_that_only_sets_type_is_not_a_mismatch():
    fragment = "<p>We survey \\textbf{forty lighthouses} and \\emph{nine} harbour lights.</p>"
    said = 'The abstract says "we survey forty lighthouses and nine harbour lights" in all.'
    assert check(said, a_short_page(fragment)).status == FOUND


def test_words_the_page_holds_in_an_attribute_are_in_the_page():
    fragment = '<img src="/plan.svg" alt="A plan of the harbour with the lighthouse at its mouth">'
    said = 'The image is described as "a plan of the harbour with the lighthouse at its mouth".'
    assert check(said, a_short_page(fragment)).status == FOUND


def test_a_passage_that_is_the_prompts_own_words_is_not_looked_for_on_the_page():
    asked = 'Does the page say "the lighthouse was sold to a private owner in 1987"? Quote it.'
    said = 'No. The page does not say "the lighthouse was sold to a private owner in 1987".'
    outcome = check(said, a_page(), prompt=asked)
    assert outcome.status == NOT_RUN
    assert outcome.reason == "the answer quotes only words of the prompt, which are not the page's"
    assert not outcome.flagged
    # Without the prompt the same passage is a claim about the page, and a false one.
    assert check(said, a_page()).status == NOT_FOUND


def test_a_redirect_notice_repeating_the_prompt_raises_nothing():
    asked = "Report the year the lighthouse was built, and quote the sentence that gives it."
    said = (
        "The page redirected to another host. Please use WebFetch again with these parameters:\n"
        f'- url: "https://example.org/lighthouse"\n- prompt: "{asked}"'
    )
    outcome = check(said, a_page(), prompt=asked)
    assert (outcome.status, outcome.flagged) == (NOT_RUN, False)


def test_a_passage_beside_one_from_the_prompt_is_still_checked():
    asked = 'Is it true that "the lighthouse was sold to a private owner in 1987"?'
    said = (
        'The page does not say "the lighthouse was sold to a private owner in 1987". It says '
        '"the last keeper retired to the mainland with his three dogs" and nothing of a sale.'
    )
    outcome = check(said, a_page(), prompt=asked)
    assert (outcome.status, outcome.quotations) == (NOT_FOUND, 1)
    assert outcome.not_found == ("the last keeper retired to the mainland with his three dogs",)


@pytest.mark.parametrize("prompt", [None, 7, ["a list"], ""])
def test_a_prompt_that_is_not_text_excludes_nothing(prompt):
    said = 'It says "the last keeper retired to the mainland with his three dogs" in passing.'
    assert check(said, a_page(), prompt=prompt).status == NOT_FOUND


REGISTER = {
    "title": "Register of lights",
    "entries": [
        {
            "name": "Marrow Point",
            "note": 'The keeper\u2019s log calls it "the most stubborn light"\nand says no more.',
            "built": 1871,
        },
        {
            "name": "Skerry Holm",
            "note": "<p>Rebuilt in <b>1934</b> after the gale, and lit by oil until the war.</p>",
            "history": "x" * 400,
        },
    ],
    "abstract_inverted_index": {
        "The": [0],
        "register": [1],
        "lists": [2],
        "every": [3],
        "light": [4, 9],
        "north": [5],
        "of": [6],
        "the": [7],
        "last": [8],
    },
}


@pytest.mark.parametrize(
    "said",
    [
        # an escaped mark, an escaped apostrophe and an escaped line end, as the characters
        "It says \"the keeper's log calls it 'the most stubborn light' and says no more\".",
        # markup inside a value
        'It says the light was "rebuilt in 1934 after the gale, and lit by oil until the war".',
        # an abstract served as an inverted index
        'The abstract reads "the register lists every light north of the last light" in full.',
    ],
)
def test_json_is_checked_against_its_string_values(said):
    page = a_page(body=json.dumps(REGISTER).encode(), ctype="application/json; charset=utf-8")
    outcome = check(said, page)
    assert (outcome.status, outcome.checked_against) == (FOUND, "retrieved.extract/json")
    # The digest and its extractor are the capture's, and say what they said before.
    assert page.extractor == "retrieved.normalize/raw"
    assert page.text_sha256 == hashlib.sha256(normalize(page.body, "").encode()).hexdigest()


def test_a_passage_no_json_value_holds_is_not_found_and_a_key_is_not_a_value():
    page = a_page(body=json.dumps(REGISTER).encode(), ctype="application/json")
    said = 'It says the light was "rebuilt in 1943 after the gale, and lit by oil until the war".'
    assert check(said, page).status == NOT_FOUND
    keys = {"a passage that is only a key of the document": "x" * 500}
    said = 'The document holds "a passage that is only a key of the document" somewhere.'
    assert check(said, a_page(body=json.dumps(keys).encode(), ctype="application/json")).status == (
        NOT_FOUND
    )


def test_json_that_does_not_parse_is_not_run():
    body = b'{"text": "the last keeper retired to the mainland with his two dogs"}' * 20
    said = 'It says "the last keeper retired to the mainland with his two dogs" in passing.'
    outcome = check(said, a_page(body=body, ctype="application/json"))
    assert outcome.status == NOT_RUN
    assert outcome.reason == "the page is served as JSON and does not parse as it"
    assert outcome.page_text_chars is None
    assert not outcome.flagged


def test_a_long_answer_over_a_small_json_document_is_not_an_answer_without_page_text():
    said = "The endpoint reports a count of 1871 and a status of open. " * 20
    assert len(said) >= answer.LONG_ANSWER_CHARS
    outcome = check(
        said, a_page(body=b'{"count": 1871, "status": "open"}', ctype="application/json")
    )
    assert outcome.status == NOT_RUN
    assert "too few to check against" in outcome.reason
    assert not outcome.answer_without_page_text
    assert not outcome.flagged


FEED = (
    '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
    "<title>Notices to mariners</title>"
    "<entry><title>Marrow Point light: reduced range until the lens is reset</title>"
    "<summary>The light at Marrow Point shows at reduced range, eleven miles in place of "
    "nineteen, until the lens is reset in the first week of May.</summary></entry>"
    "<entry><title>Skerry Holm</title><summary><![CDATA[The <b>afternoon</b> sailing to "
    "Skerry Holm is cancelled whenever the light fails before four.]]></summary></entry>"
    "<entry><title>Fairway buoy</title><content type='html'>&lt;p&gt;The fairway buoy was "
    "&lt;em&gt;relaid&lt;/em&gt; forty metres east of its charted position.&lt;/p&gt;</content>"
    f"</entry><entry><summary>{'Further notices follow in the next issue. ' * 6}</summary></entry>"
    "</feed>"
).encode()


@pytest.mark.parametrize("ctype", ["application/atom+xml; charset=utf-8", "text/xml"])
@pytest.mark.parametrize(
    "said",
    [
        'One entry is titled "Marrow Point light: reduced range until the lens is reset" now.',
        'It says the light "shows at reduced range, eleven miles in place of nineteen" for now.',
        # inside a CDATA section, with a tag in it
        'It says "the afternoon sailing to Skerry Holm is cancelled" when the light fails.',
        # an entry carried as escaped HTML
        'It says "the fairway buoy was relaid forty metres east of its charted position" too.',
    ],
)
def test_xml_is_checked_with_its_tags_removed(ctype, said):
    outcome = check(said, a_page(body=FEED, ctype=ctype))
    assert (outcome.status, outcome.checked_against) == (FOUND, "retrieved.extract/xml")


def test_an_element_inside_a_sentence_of_xml_is_removed_from_it():
    body = (
        "<notices><notice>The light at <place>Marrow Point</place> shows at reduced range "
        f"until the lens is reset.</notice><notice>{'No other notice is in force. ' * 14}"
        "</notice></notices>"
    ).encode()
    said = 'It says "the light at Marrow Point shows at reduced range until the lens is reset".'
    assert check(said, a_page(body=body, ctype="application/xml")).status == FOUND


def test_a_passage_an_xml_feed_does_not_hold_is_not_found():
    said = 'It says the light "shows at reduced range, seven miles in place of nineteen" for now.'
    assert check(said, a_page(body=FEED, ctype="application/atom+xml")).status == NOT_FOUND


def a_pdf(lines):
    """One page of text set in Helvetica, written out by hand so that no test needs a library
    or a file anyone else wrote."""
    text = b"BT /F1 11 Tf 72 720 Td 14 TL " + b" ".join(
        b"(" + line.encode() + b") Tj T*" for line in lines
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s ET\nendstream" % (len(text) + 3, text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        out.index(b"xref\n0 "),
    )


# Written for these tests. Two lines end in a hyphen: one splits a word, one is a compound's own.
TIDE_TABLES = a_pdf(
    [
        "The tide tables for Skerry Holm are printed each January by the har-",
        "bour office, and a copy is posted at the head of the old quay.",
        "Low water at the spring tides uncovers a well-",
        "known causeway to the islet for about two hours.",
        "Nobody should cross it on a rising tide, and the board has said so",
        "on a painted notice at either end since the drowning of 1911.",
        "The tables give the height of high water to a tenth of a metre,",
        "and the times in local time with no correction for summer.",
        "A printed copy costs four pounds from the office or the chandlery.",
    ]
)

needs_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="pdftotext is not installed here"
)


@needs_pdftotext
@pytest.mark.parametrize(
    "said",
    [
        'It says "a copy is posted at the head of the old quay" each year.',
        # across a line end
        'It says "the times in local time with no correction for summer. A printed copy costs four '
        'pounds" there.',
        # a word the page splits with a hyphen at a line end
        'It says the tables "are printed each January by the harbour office" every year.',
        # a compound whose own hyphen falls at a line end
        'It says low water "uncovers a well-known causeway to the islet for about two hours".',
    ],
)
@pytest.mark.parametrize("ctype", ["application/pdf", "application/octet-stream"])
def test_a_pdf_is_checked_against_the_text_pdftotext_extracts(said, ctype):
    page = a_page(body=TIDE_TABLES, ctype=ctype)
    outcome = check(said, page)
    assert (outcome.status, outcome.quotations, outcome.found) == (FOUND, 1, 1)
    assert outcome.checked_against.startswith("pdftotext")
    assert outcome.page_text_chars >= answer.SPARSE_PAGE_CHARS
    # Only the text the answer is checked against. The record's own digest and extractor are
    # the capture's and still describe the bytes decoded as they are.
    assert page.extractor == "retrieved.normalize/raw"
    assert page.text_sha256 == hashlib.sha256(normalize(TIDE_TABLES, ctype).encode()).hexdigest()


@needs_pdftotext
def test_the_extractor_is_named_with_its_version():
    said = 'It says "a copy is posted at the head of the old quay" each year.'
    named = check(said, a_page(body=TIDE_TABLES, ctype="application/pdf")).checked_against
    reported = subprocess.run(
        [shutil.which("pdftotext"), "-v"], capture_output=True, check=False
    ).stderr.decode()
    assert named.split()[0] == "pdftotext"
    assert named.split()[1] in reported


@needs_pdftotext
@pytest.mark.parametrize(
    "said",
    [
        'It says "a copy is posted at the foot of the old quay" each year.',
        'It says low water "uncovers a well-known causeway to the islet for about six hours".',
    ],
)
def test_a_passage_a_pdf_does_not_hold_is_not_found(said):
    outcome = check(said, a_page(body=TIDE_TABLES, ctype="application/pdf"))
    assert outcome.status == NOT_FOUND
    assert outcome.flagged


@needs_pdftotext
def test_a_pdf_pdftotext_cannot_read_is_not_run_and_never_a_mismatch():
    said = 'It says "a copy is posted at the head of the old quay" each year.'
    body = b"%PDF-1.7 a copy is posted at the head of the old quay " * 20
    outcome = check(said, a_page(body=body, ctype="application/pdf"))
    assert outcome.status == NOT_RUN
    assert outcome.reason.startswith("pdftotext could not read the PDF (exit ")
    assert (outcome.page_text_chars, outcome.checked_against) == (None, "")
    assert not outcome.flagged


def test_a_pdf_with_no_pdftotext_on_the_path_is_not_run(monkeypatch):
    monkeypatch.setenv("PATH", "")
    said = 'It says "a copy is posted at the foot of the old quay" each year. ' * 20
    outcome = check(said, a_page(body=TIDE_TABLES, ctype="application/pdf"))
    assert outcome.status == NOT_RUN
    assert outcome.reason == "the page is a PDF and no pdftotext is installed to read it"
    assert (outcome.page_text_chars, outcome.checked_against) == (None, "")
    assert (outcome.quotations, outcome.not_found) == (0, ())
    assert not outcome.answer_without_page_text
    assert not outcome.flagged


def an_extractor_that(monkeypatch, behaves):
    """Stand in for the program, so what it can do wrong is tested where it is not installed."""
    monkeypatch.setattr(extract.shutil, "which", lambda name: "/opt/absent/pdftotext")
    monkeypatch.setattr(extract.subprocess, "run", behaves)


def test_a_pdftotext_that_runs_out_of_time_is_not_run(monkeypatch):
    def hangs(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    an_extractor_that(monkeypatch, hangs)
    said = 'It says "a copy is posted at the foot of the old quay" each year.'
    outcome = check(said, a_page(body=TIDE_TABLES, ctype="application/pdf"))
    assert (outcome.status, outcome.flagged) == (NOT_RUN, False)
    assert outcome.reason == (
        f"pdftotext did not finish in {extract.PDFTOTEXT_TIMEOUT:.0f} seconds"
    )


def test_a_pdftotext_that_cannot_be_started_is_not_run(monkeypatch):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    an_extractor_that(monkeypatch, missing)
    said = 'It says "a copy is posted at the foot of the old quay" each year.'
    outcome = check(said, a_page(body=TIDE_TABLES, ctype="application/pdf"))
    assert (outcome.status, outcome.flagged) == (NOT_RUN, False)
    assert outcome.reason == "pdftotext could not be run: FileNotFoundError"


def test_a_pdf_with_no_text_layer_is_not_run_and_is_not_an_answer_without_page_text(monkeypatch):
    def finds_nothing(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=b"\n\n\x0c", stderr=b"")

    an_extractor_that(monkeypatch, finds_nothing)
    said = 'It says "a copy is posted at the foot of the old quay" each year. ' * 20
    outcome = check(said, a_page(body=TIDE_TABLES, ctype="application/pdf"))
    assert outcome.status == NOT_RUN
    assert outcome.reason == "pdftotext found no text in the PDF, which may be scanned pages"
    assert not outcome.answer_without_page_text
    assert not outcome.flagged


def test_the_pdf_is_handed_to_pdftotext_as_a_file_that_is_gone_afterwards(monkeypatch):
    seen = {}

    def reads(command, **kwargs):
        if command[-1] == "-v":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"version 1.0")
        seen["command"] = command
        with open(command[-2], "rb") as handle:
            seen["bytes"] = handle.read()
        return subprocess.CompletedProcess(command, 0, stdout=b"The tide tables. " * 40, stderr=b"")

    an_extractor_that(monkeypatch, reads)
    check("Nothing is quoted here.", a_page(body=TIDE_TABLES, ctype="application/pdf"))
    assert seen["bytes"] == TIDE_TABLES
    assert "-layout" not in seen["command"]
    with pytest.raises(FileNotFoundError):
        open(seen["command"][-2], "rb")


def test_a_truncated_pdf_is_not_handed_to_the_extractor(monkeypatch):
    def must_not_run(command, **kwargs):
        raise AssertionError("pdftotext was run on a truncated PDF")

    an_extractor_that(monkeypatch, must_not_run)
    page = a_page(body=TIDE_TABLES + b"0" * MAX_BYTES, ctype="application/pdf")
    assert page.truncated
    assert "truncated" in check("Nothing is quoted here.", page).reason


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


def test_a_long_answer_over_a_page_with_almost_no_text_is_recorded_and_tells_nobody():
    said = "The lighthouse was built in 1871. " * 30
    assert len(said) >= answer.LONG_ANSWER_CHARS
    outcome = check(said, a_page(body=WALL))
    assert outcome.answer_without_page_text
    assert outcome.as_record()["answer_without_page_text"] is True
    assert not outcome.flagged
    # Its own measure: no quotation was checked, so none is reported as failing.
    assert outcome.status == NOT_RUN
    assert "a sign-in page" in outcome.reason
    assert outcome.not_found == ()
    assert outcome.answer_chars == len(said)
    assert outcome.page_text_chars == len("Sign in Please sign in to continue reading.")


@pytest.mark.parametrize("status", [403, 404, 429, 500, 301, 199])
def test_a_refused_fetch_is_could_not_check_and_never_a_doubt_about_the_answer(status):
    # The whole article comes back under an error status, with a passage the answer misquotes
    # and an answer long enough to be measured against the page. None of it is looked at.
    said = 'It says "the first keeper retired to the mainland with his two dogs" there. ' * 12
    outcome = check(said, a_page(status=status), tool_status=200)
    assert outcome.status == NOT_RUN
    assert outcome.reason == (
        f"the independent fetch was refused (HTTP {status}); the page the tool read was not "
        "obtained"
    )
    assert (outcome.quotations, outcome.not_found) == (0, ())
    assert (outcome.page_text_chars, outcome.checked_against) == (None, "")
    assert not outcome.answer_without_page_text
    assert not outcome.flagged
    assert outcome.answer_chars == len(said)


@pytest.mark.parametrize("tool_status", [None, 403, 0, "200", True, 200.0])
def test_an_error_page_is_not_checked_whatever_the_tool_reported(tool_status):
    said = 'It says "the first keeper retired to the mainland with his two dogs" in passing.'
    outcome = check(said, a_page(status=404), tool_status=tool_status)
    assert (outcome.status, outcome.flagged) == (NOT_RUN, False)
    assert outcome.reason == (
        "the independent fetch returned HTTP 404, and an error page is not the page"
    )


@pytest.mark.parametrize("status", [200, 203, 299])
def test_a_page_served_with_success_is_checked(status):
    said = 'It says "the first keeper retired to the mainland with his two dogs" in passing.'
    assert check(said, a_page(status=status), tool_status=200).status == NOT_FOUND


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
