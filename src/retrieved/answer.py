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
3. Pure. Nothing here reads a file, writes one or makes a request, so the hook can call it with
   nothing to clean up and the tests can call it with nothing to mock.

The page is the one this library fetched, which is not necessarily the one the fetch tool read. A
passage reported missing is missing from this retrieval, and everything said about it says so.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass

from retrieved.capture import MAX_BYTES, Retrieval, normalize

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
# (` and then `) never satisfies. The lookarounds keep `5" wide` from opening anything.
_STRAIGHT = re.compile(r'(?<!\w)"([^"\s](?:[^"\n]*[^"\s])?)"(?!\w)')

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
    }
)
_SPACE = re.compile(r"\s+")
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
    answer_without_page_text: bool = False
    answer_chars: int = 0
    #: None where the page is not text this library extracts, which is not the same as zero.
    page_text_chars: int | None = None
    answer_sha256: str = ""

    @property
    def flagged(self) -> bool:
        """Whether the agent should be told. Never true merely because the check did not run."""
        return self.status == NOT_FOUND or self.answer_without_page_text

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
            "answer_sha256": self.answer_sha256,
        }


def quotations(answer: str) -> list[str]:
    """The passages an answer presents as quoted from the page, in the order they are found.

    Text inside curly or straight double quotation marks, long enough to be a passage, not marked
    as edited, and plainly prose: a string with no space in it is a URL, an identifier or a
    filename, and none of those is a quotation however it is punctuated.
    """
    found: list[str] = []
    for pattern in (_CURLY, _STRAIGHT):
        for match in pattern.finditer(answer):
            passage = match.group(1).strip()
            if len(passage) < MIN_QUOTATION_CHARS:
                continue
            if any(mark in passage for mark in _EDIT_MARKS):
                continue
            if not any(character.isspace() for character in passage):
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
    forms, curly marks, the several dashes, soft hyphens, case.

    Spaces before closing punctuation go too. `normalize` replaces every tag with a space, so
    `<a>the report</a>, which` reads `the report , which` on the page side and never on the
    answer's.
    """
    text = unicodedata.normalize("NFKC", html.unescape(text)).translate(_TO_ASCII)
    text = _SPACE.sub(" ", text).strip()
    text = _BEFORE_PUNCTUATION.sub(r"\1", text)
    text = _AFTER_OPENING.sub(r"\1", text)
    return text.casefold()


def occurs(passage: str, folded_page: str) -> bool:
    """Whether a passage is in the page, given the page already folded."""
    needle = fold(passage).rstrip(_TRAILING)
    return bool(needle) and needle in folded_page


def _is_text(retrieval: Retrieval) -> bool:
    """Whether `normalize` extracts this page's text, or only decodes its bytes.

    HTML, and anything served as `text/*`. A PDF goes through the raw extractor and comes out as
    the bytes of a PDF, in which no sentence appears however faithfully it was quoted. JSON and
    XML are left out for the same reason in a milder form: their escaping and markup sit inside
    the text, so a passage can be present and not contiguous.
    """
    if retrieval.extractor.endswith("/html"):
        return True
    return retrieval.content_type.strip().lower().startswith("text/")


def check(answer: object, retrieval: Retrieval) -> AnswerCheck:
    """Check the fetch tool's answer against a retrieval of the same URL.

    `answer` is whatever the hook payload held, which is why it is typed as an object: anything
    that is not a non-empty string is the check not running, with that as its reason.
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

    if not _is_text(retrieval):
        served_as = retrieval.content_type or "no stated content type"
        return AnswerCheck(
            NOT_RUN,
            reason=f"the page is {served_as}, which is not text this library extracts",
            **about_the_answer,
        )
    if retrieval.truncated:
        return AnswerCheck(
            NOT_RUN,
            reason=f"the page was truncated at {MAX_BYTES} bytes, and a passage beyond that "
            f"would read as missing",
            **about_the_answer,
        )

    text = normalize(retrieval.body, retrieval.content_type)
    if len(text) < SPARSE_PAGE_CHARS:
        return AnswerCheck(
            NOT_RUN,
            reason=f"the page holds {len(text)} characters of text, too few to check against",
            answer_without_page_text=len(answer) >= LONG_ANSWER_CHARS,
            page_text_chars=len(text),
            **about_the_answer,
        )

    passages = quotations(answer)
    if not passages:
        # Not "found": nothing was looked for, and zero of zero is not a clean result.
        return AnswerCheck(
            NOT_RUN,
            reason=f"the answer quotes nothing of {MIN_QUOTATION_CHARS} characters or more",
            page_text_chars=len(text),
            **about_the_answer,
        )

    page = fold(text)
    missing = tuple(passage for passage in passages if not occurs(passage, page))
    return AnswerCheck(
        NOT_FOUND if missing else FOUND,
        quotations=len(passages),
        found=len(passages) - len(missing),
        not_found=missing,
        page_text_chars=len(text),
        **about_the_answer,
    )


__all__ = ["FOUND", "NOT_FOUND", "NOT_RUN", "AnswerCheck", "check", "fold", "occurs", "quotations"]
