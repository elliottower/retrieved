"""Check a fetch tool's answer against the page that was just retrieved.

The fetch tool hands the agent a model's answer and never the page, so a passage that answer puts
in quotation marks is a claim about text the agent has not seen. Once the page has been retrieved
that claim can be checked without judgement: the words are in the page or they are not.

Design constraints, in order:

1. Deterministic. Only what the answer presents as a quotation is checked, by containment after a
   normalization that preserves which words appear. Whether a paraphrase is fair is a semantic
   question, and a tool that guesses at one gets believed when it is wrong.
2. Three outcomes, kept apart: every passage was found, some were not, or the check did not run.
   "Did not run" carries its reason and must never read as "nothing wrong" -- a PDF this library
   cannot extract and a page that supports every quotation are different facts about a record.
3. Nothing kept. Nothing here writes a file or makes a request, so the hook can call it with
   nothing to clean up. The page's text comes from `extract`, which runs one program where the
   page is a PDF, and a page whose text cannot be had is a check that did not run.
4. Loosened only where a typesetter is the difference. A passage the plain comparison misses
   is looked for once more without spaces and without the marks markup leaves behind, because
   a tag or a line end puts a space inside a word and an answer writes emphasis the page does
   not. Nothing is loosened that could let a different word or number through.
5. A false note costs more than a missed one. A quoted string that is the prompt's own words,
   an attribute's value or a reproduced block of a document is not looked for at all.

The page is the one this library fetched, which is not necessarily the one the fetch tool read. A
passage reported missing is missing from this retrieval, and everything said about it says so.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass

from retrieved.capture import MAX_BYTES, Retrieval
from retrieved.extract import HTML, PDF, TEXT, extract
from retrieved.wall import recognise

#: Shorter than this and a quoted string is a term, a title or a label rather than a passage, and
#: a few words can occur on a page by accident where a sentence cannot.
MIN_QUOTATION_CHARS = 30

#: How much of a passage that was not found is kept, in the record and in `flags.jsonl`. Enough
#: to find the place in the answer; the answer itself is not stored, only its digest.
PASSAGE_CAP = 300

#: Below this much extracted text the page did not come with its content: a login wall, an error
#: page, a JavaScript shell nothing rendered. The same figure `render` uses to recognise a shell,
#: for the same reason. Quotations are not checked against such a page, because a re-fetch that
#: met a wall where the fetch tool met an article would report every one of them missing.
SPARSE_PAGE_CHARS = 400

#: An answer at least this long cannot have come from a page holding under SPARSE_PAGE_CHARS of
#: text: it is twice the most such a page can say. A short answer over a short page ("the page
#: asks you to sign in") is the fetch tool working, so length alone on either side is no signal.
LONG_ANSWER_CHARS = 2 * SPARSE_PAGE_CHARS

FOUND = "found"
NOT_FOUND = "not_found"
NOT_RUN = "not_run"

_CURLY = re.compile(r"“([^“”]*)”")

# A straight mark opens and closes with the same character, so a pattern that only looks for two
# of them will pair the close of one quotation with the open of the next and capture the prose
# between. Two things prevent it. Every pair is consumed whatever its length, and the length is
# judged afterwards, so a short quotation still uses up its own marks. And an opening mark must
# be followed by text and a closing mark preceded by it, which the text between two quotations
# (` and then `) never satisfies. The lookarounds keep `5" wide` from opening anything, and
# `alt="a diagram of the harbour"` too: a mark straight after `=` opens an attribute's value,
# which is what an answer holds when the fetch tool hands back a page's markup unread.
#
# A quotation may run over a line end, because an answer wraps its lines and quotes a list item
# or a sentence whole. It may not run over a blank line: a stray mark then pairs with nothing
# further away than the end of its own paragraph.
_STRAIGHT = re.compile(r'(?<![\w=])"([^"\s](?:(?:[^"\n]|\n(?![ \t]*\n))*[^"\s])?)"(?!\w)')

# A markdown block quote: up to three spaces, the mark, and the line. The lines of one block
# are one passage, and a line holding only the mark ends it as a blank line ends a paragraph.
_BLOCK_LINE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
_BLOCK_MARK = re.compile(r"^ {0,3}>[ \t]?", re.M)

# What a line of a block starts with when the block is a piece of a document and not a passage:
# a heading, a list item, a table row, a code fence, a block quote of its own.
_STRUCTURE = re.compile(r"#{1,6} |[-*+] |\d+[.)] |\||```|>")

#: Emphasis an answer wraps a quoted block in. It is the answer's and never the page's.
_EMPHASIS = "*_ "

#: An ellipsis or a square bracket marks a quotation its author edited, so the passage is not
#: claimed to appear on the page as written and cannot be checked as though it were.
_EDIT_MARKS = ("...", "…", "[", "]")

_TO_ASCII = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "­": None,  # soft hyphen: a line-breaking hint, invisible and not part of any word
        # Zero-width and invisible format characters. A page sets them between a function's name
        # and its bracket or at a permitted line break, and nothing shows where one is.
        "\u200b": None,
        "\u200c": None,
        "\u200d": None,
        "\u2060": None,
        "\u2061": None,
        "\u2062": None,
        "\u2063": None,
        "\u2064": None,
        "\ufeff": None,
    }
)
_SPACE = re.compile(r"\s+")
# Two hyphens or three are how plain text and TeX source write a dash.
_HYPHENS = re.compile(r"-{2,}")
# What markup leaves inside a sentence without being a word of it: the spaces a removed tag
# becomes, markdown's emphasis and code marks, the backslash of an escape, and quotation marks,
# which an answer changes from double to single when it nests a quotation inside its own.
_TYPESETTING = re.compile(r"[\s*_`\\\"']+")
# A hyphen with a letter on each side. Never one beside a digit, where it is a minus sign or a
# range and removing it would make `-5` read as `5`.
_JOINING_HYPHEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")
_BEFORE_PUNCTUATION = re.compile(r" ([.,;:!?)\]])")
_AFTER_OPENING = re.compile(r"([(\[]) ")

#: Ignored at the end of a passage. An answer commonly closes a quotation with a full stop or a
#: comma where the page's sentence carries on, and that is punctuation of the answer's sentence
#: rather than a claim about the page's.
_TRAILING = " .,;:"


@dataclass(frozen=True)
class AnswerCheck:
    """What checking one answer against one retrieval established, and what it could not."""

    status: str
    reason: str = ""
    quotations: int = 0
    found: int = 0
    not_found: tuple[str, ...] = ()
    #: Separate from the quotations on purpose. It can be true when no passage was checked at
    #: all, and reporting it as a quotation that failed would say something that was not measured.
    #: Recorded and never told to the agent. Over 1,407 past answers it was true of 35, and on
    #: none was the answer at fault: the second fetch had met a wall the first did not, or the
    #: answer itself said the page was empty. A note that is wrong whenever it is given teaches
    #: its reader to pass over the notes that are right.
    answer_without_page_text: bool = False
    answer_chars: int = 0
    #: None where the page is not text this library extracts, which is not the same as zero.
    page_text_chars: int | None = None
    #: What produced the text the passages were looked for in: `retrieved.normalize/html`,
    #: `pdftotext 24.02.0`. Empty where there was no text, and then `reason` says why.
    checked_against: str = ""
    answer_sha256: str = ""

    @property
    def flagged(self) -> bool:
        """Whether the agent should be told: a quoted passage is not in the page, and only that.

        Never true merely because the check did not run, and never for an answer longer than
        its page's text, which is a fact about the second fetch more often than about the
        answer.
        """
        return self.status == NOT_FOUND

    def as_record(self) -> dict[str, object]:
        """The block a retrieval's record carries. The digest of the answer, never the answer."""
        return {
            "status": self.status,
            "reason": self.reason,
            "quotations": self.quotations,
            "found": self.found,
            "not_found": [passage[:PASSAGE_CAP] for passage in self.not_found],
            "answer_without_page_text": self.answer_without_page_text,
            "answer_chars": self.answer_chars,
            "page_text_chars": self.page_text_chars,
            "checked_against": self.checked_against,
            "answer_sha256": self.answer_sha256,
        }


def _blocks(answer: str) -> list[str]:
    """The markdown block quotes in an answer, each as one passage with its marks removed.

    A block that has quotation marks inside it is left to them: the marks say which words are
    the page's, and the rest of the block is attribution or comment the answer added.
    """
    blocks: list[str] = []
    lines: list[str] = []
    for line in [*answer.splitlines(), ""]:
        match = _BLOCK_LINE.match(line)
        if match and match.group(1).strip():
            lines.append(match.group(1).strip())
            continue
        # A block holding a heading, a list or a table is a piece of a document reproduced with
        # its structure, which is what an answer is when the fetch tool hands a markdown page
        # back whole. Its marks are not the page's words, and it is not offered as a quotation.
        if lines and not any(_STRUCTURE.match(inside) for inside in lines):
            blocks.append(" ".join(lines).strip(_EMPHASIS))
        lines = []
    return [block for block in blocks if not any(mark in block for mark in '"“”')]


def quotations(answer: str) -> list[str]:
    """The passages an answer presents as quoted from the page, in the order they are found.

    Text inside curly or straight double quotation marks, and markdown block quotes. Each must
    be long enough to be a passage, not marked as edited, and plainly prose: a string with no
    space in it is a URL, an identifier or a filename, and none of those is a quotation however
    it is punctuated.

    Single quotation marks are not read. The same character is the apostrophe, so `the page's
    'first' and the keeper's` pairs wherever the next one happens to fall.
    """
    # Block-quote marks come off first, so a quotation in marks that runs over two lines of a
    # block does not carry the second line's `>` into the passage.
    unmarked = _BLOCK_MARK.sub("", answer)
    candidates = [
        match.group(1) for pattern in (_CURLY, _STRAIGHT) for match in pattern.finditer(unmarked)
    ]
    found: list[str] = []
    for candidate in [*candidates, *_blocks(answer)]:
        # A line end inside a quotation is the answer's wrapping, and is reported as a space.
        passage = _SPACE.sub(" ", candidate).strip()
        if len(passage) < MIN_QUOTATION_CHARS:
            continue
        if any(mark in passage for mark in _EDIT_MARKS):
            continue
        if " " not in passage:
            continue
        if passage not in found:
            found.append(passage)
    return found


def fold(text: str) -> str:
    """Text reduced to which words appear, in what order.

    Applied to both sides of the comparison, on top of `capture.normalize` for the page. The two
    differ deliberately. `normalize` is what the text digest is taken over, so it does as little
    as it can and must never change: it keeps case, entities and typography, because a digest
    that moved whenever this function was improved would report drift on pages that had not
    drifted. This asks a looser question -- are these words on the page -- of two texts typeset
    by different hands, so it removes what typesetting adds: entity encoding, compatibility
    forms, curly marks, the several dashes and a run of hyphens standing for one, soft hyphens,
    case.

    Spaces before closing punctuation go too. `normalize` replaces every tag with a space, so
    `<a>the report</a>, which` reads `the report , which` on the page side and never on the
    answer's.
    """
    text = unicodedata.normalize("NFKC", html.unescape(text)).translate(_TO_ASCII)
    text = _HYPHENS.sub("-", text)
    text = _SPACE.sub(" ", text).strip()
    text = _BEFORE_PUNCTUATION.sub(r"\1", text)
    text = _AFTER_OPENING.sub(r"\1", text)
    return text.casefold()


def squeeze(folded: str, *, typeset: bool = False) -> str:
    """Folded text with what markup leaves inside a sentence removed, spaces first among it.

    `normalize` puts a space wherever a tag was, so `word<b>s</b>` reads `word s` and a passage
    quoting `words` is not in the page as folded. Without spaces the two agree. The fetch tool
    reads a page as markdown, so its answer carries `_emphasis_` and `\\*` where the page has a
    tag or nothing, and an answer nesting a quotation writes `'exactly'` where the page has
    `"exactly"`. Those marks go from both sides as well. Letters, digits and every other mark
    stay, a full stop and a minus sign among them.

    What this gives up is the word boundaries: `a nice box` and `an icebox` squeeze to the same
    letters. Over a passage of MIN_QUOTATION_CHARS or more, every letter and mark in order, a
    second wording with the same letters and different boundaries is not something a page holds
    by accident, and that length is why the minimum is not lowered.

    `typeset` is for a PDF. A line there can end in a hyphen that splits a word, and
    `pdftotext` rejoins such a line by dropping the hyphen, whether the word was split
    (`har-bour`) or is written with one (`well-known`). So a hyphen between two letters is
    removed on both sides, and `well-known` in an answer meets `wellknown` in the text. A hyphen
    beside a digit stays: it may be a minus sign.
    """
    text = _TYPESETTING.sub("", folded)
    return _JOINING_HYPHEN.sub("", text) if typeset else text


def occurs(passage: str, folded_page: str, *, typeset: bool = False) -> bool:
    """Whether a passage is in the page, given the page already folded.

    As folded first, and squeezed only where that fails, so the looser comparison never decides
    a passage the stricter one can.
    """
    needle = fold(passage).rstrip(_TRAILING)
    if not needle:
        return False
    if needle in folded_page:
        return True
    # Trailing punctuation again, because a closing mark inside the passage hid it the first
    # time: `joints.'` ends in a full stop only once the quotation mark is gone.
    squeezed = squeeze(needle, typeset=typeset).rstrip(_TRAILING)
    return bool(squeezed) and squeezed in squeeze(folded_page, typeset=typeset)


def _succeeded(status: object) -> bool:
    return type(status) is int and 200 <= status < 300


def check(
    answer: object, retrieval: Retrieval, *, prompt: object = "", tool_status: object = None
) -> AnswerCheck:
    """Check the fetch tool's answer against a retrieval of the same URL.

    `answer` is whatever the hook payload held, which is why it is typed as an object: anything
    that is not a non-empty string is the check not running, with that as its reason.

    `prompt` is what the agent asked of the page. A passage the answer quotes that is in the
    prompt is the agent's own wording handed back -- a title it asked after, a phrase the answer
    says the page does not use, the whole prompt repeated in the fetch tool's notice of a
    redirect -- and is not a claim about the page, so it is not looked for there.

    `tool_status` is the HTTP status the fetch tool reported for its own request, where the
    payload gave one. It decides nothing but the wording of one reason.
    """
    if not isinstance(answer, str) or not answer.strip():
        return AnswerCheck(NOT_RUN, reason="no answer in the payload")

    # JSON can carry half of a surrogate pair, which is a string Python holds and cannot encode.
    # Replaced here so that neither the digest below nor the record written later can raise.
    answer = answer.encode("utf-8", errors="replace").decode("utf-8")
    about_the_answer = {
        "answer_chars": len(answer),
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
    }

    # An error page is not the page. A server that refuses this library and served the fetch
    # tool returns a 403 with a paragraph of its own, or a 404 with the site's whole navigation,
    # and a passage looked for there is reported missing from a page nobody quoted. So nothing
    # is looked for, and where the tool's own request succeeded the reason says outright that
    # the page it read was not obtained: that is "could not check", not a doubt about the answer.
    if not _succeeded(retrieval.http_status):
        got = f"HTTP {retrieval.http_status}"
        return AnswerCheck(
            NOT_RUN,
            reason=f"the independent fetch was refused ({got}); the page the tool read was not "
            f"obtained"
            if _succeeded(tool_status)
            else f"the independent fetch returned {got}, and an error page is not the page",
            **about_the_answer,
        )

    # Before extraction, because half a PDF does not parse and half a JSON document does not
    # either, and "truncated" is the truer reason for both.
    if retrieval.truncated:
        return AnswerCheck(
            NOT_RUN,
            reason=f"the page was truncated at {MAX_BYTES} bytes, and a passage beyond that "
            f"would read as missing",
            **about_the_answer,
        )
    extracted = extract(retrieval)
    if extracted.text is None:
        return AnswerCheck(NOT_RUN, reason=extracted.reason, **about_the_answer)
    about_the_page = {
        "page_text_chars": len(_SPACE.sub(" ", extracted.text).strip()),
        "checked_against": extracted.by,
    }

    # A wall served with success is not the page either, and unlike a page that is merely short
    # it can run to a thousand characters of its own. Its reason is its own: "too few
    # characters" would be true of most walls and would say nothing of what was met.
    wall = recognise(retrieval)
    if wall or about_the_page["page_text_chars"] < SPARSE_PAGE_CHARS:
        return AnswerCheck(
            NOT_RUN,
            reason=f"the independent fetch met {wall}, which is not the page the tool read"
            if wall
            else f"the page holds {about_the_page['page_text_chars']} characters of text, "
            f"too few to check against",
            # Only where the page is prose. A JSON document of numbers and a PDF whose text
            # layer is thin are short for reasons that say nothing about where the answer
            # came from.
            answer_without_page_text=extracted.kind in (HTML, TEXT)
            and about_the_page["page_text_chars"] < SPARSE_PAGE_CHARS
            and len(answer) >= LONG_ANSWER_CHARS,
            **about_the_page,
            **about_the_answer,
        )

    asked = fold(prompt) if isinstance(prompt, str) else ""
    quoted = quotations(answer)
    passages = [passage for passage in quoted if not occurs(passage, asked)]
    if quoted and not passages:
        return AnswerCheck(
            NOT_RUN,
            reason="the answer quotes only words of the prompt, which are not the page's",
            **about_the_page,
            **about_the_answer,
        )
    if not passages:
        # Not "found": nothing was looked for, and zero of zero is not a clean result.
        return AnswerCheck(
            NOT_RUN,
            reason=f"the answer quotes nothing of {MIN_QUOTATION_CHARS} characters or more",
            **about_the_page,
            **about_the_answer,
        )

    page = fold(f"{extracted.text}\n{extracted.second}")
    typeset = extracted.kind == PDF
    missing = tuple(passage for passage in passages if not occurs(passage, page, typeset=typeset))
    return AnswerCheck(
        NOT_FOUND if missing else FOUND,
        quotations=len(passages),
        found=len(passages) - len(missing),
        not_found=missing,
        **about_the_page,
        **about_the_answer,
    )


__all__ = [
    "FOUND",
    "NOT_FOUND",
    "NOT_RUN",
    "AnswerCheck",
    "check",
    "fold",
    "occurs",
    "quotations",
    "squeeze",
]
