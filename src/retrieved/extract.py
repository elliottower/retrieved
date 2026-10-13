"""The text a fetch answer is checked against, and what produced it.

Not the text a digest is taken over. `capture.normalize` is that, and it must never change: it
decodes a PDF's bytes and leaves a JSON document's escapes where they are, which is right for
asking whether a page changed and useless for asking whether a sentence is in it. This module
answers the second question only. Nothing here reaches `text_sha256` or a record's `extractor`.

Design constraints, in order:

1. Never a guess. Where the text cannot be had -- no extractor installed, one that failed or ran
   out of time, a document that does not parse, a PDF with no text layer -- there is no text,
   there is a reason, and the check does not run. A partial or mangled text reports faithful
   quotations as missing, and a false note costs more than a missed one.
2. Named. Every text comes back with what produced it, and the version where there is one,
   because "not in the page" is a statement about an extraction as much as about a page.
3. Nothing kept and nothing requested. The one program run is `pdftotext`, over bytes already in
   hand, through a temporary file that is gone before this returns.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from retrieved.capture import Retrieval, normalize

#: Long enough for a book on a slow disk, and short enough that a PDF built to hang a parser
#: costs the detached worker this and nothing else.
PDFTOTEXT_TIMEOUT = 20.0

HTML = "html"
TEXT = "text"
JSON = "json"
XML = "xml"
PDF = "pdf"

_TAG = re.compile(r"<[^>]+>")
# MathML carries each formula twice: as elements a browser draws, and as the TeX it was
# written in, inside an `annotation`. With tags stripped the two run together, `κ \kappa`.
_ANNOTATION = re.compile(r"<annotation\b.*?</annotation(?:-xml)?>", re.S | re.I)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
# TeX that only sets type: `\textbf{six models}` is the words `six models`. A listing or a
# feed carries an abstract as its author wrote it, and a reader of the rendered page quotes the
# words. Formulas are left alone: turning `\rho` into a letter is rendering, not stripping.
_TEX_TYPE = re.compile(r"\\(?:textbf|textit|texttt|textsc|textrm|emph|text)\s*\{([^{}]*)\}")
_VERSION = re.compile(r"version\s+(\S+)")


@dataclass(frozen=True)
class Extracted:
    """A page's text for the answer check, or why there is none."""

    #: None where there is nothing to check against, and then `reason` says why.
    text: str | None
    #: Which kind of page this was. The check treats them differently in two places: only HTML
    #: and plain text are pages whose near-emptiness means a wall, and only a PDF's text has
    #: had its lines joined by a typesetter and an extractor.
    kind: str = ""
    #: What produced the text, as the record gives it under `checked_against`.
    by: str = ""
    reason: str = ""
    #: The same page read a second way, searched as well as `text` and never counted with it.
    #: A passage has to be contiguous in one reading or the other, so a second can only find
    #: more than the first did.
    second: str = ""


def _mime(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def _is_inverted_index(value: dict) -> bool:
    """Whether a JSON object maps each word of a text to the positions it holds in it.

    Some APIs serve an abstract this way and no other, so its sentences are in no string value
    and its words are keys.
    """
    return bool(value) and all(
        isinstance(positions, list)
        and positions
        and all(type(position) is int for position in positions)
        for positions in value.values()
    )


def _reassembled(index: dict) -> str:
    words = sorted((position, word) for word, positions in index.items() for position in positions)
    return " ".join(word for _, word in words)


def _without_tex_type(text: str) -> str:
    """Text with type-setting TeX commands unwrapped and math delimiters removed."""
    return _TEX_TYPE.sub(r"\1", text).replace("$", "")


def _strings(value: object) -> list[str]:
    """Every string value in a parsed JSON document, in document order.

    Values and not keys: a key is a field's name, and nobody quotes a page by its field names.
    The one exception is an inverted index, whose keys are the text.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if _is_inverted_index(value):
            return [_reassembled(value)]
        return [found for item in value.values() for found in _strings(item)]
    if isinstance(value, list):
        return [found for item in value for found in _strings(item)]
    return []


def _from_json(body: bytes) -> Extracted:
    """The string values of a JSON document, one to a line.

    Parsed rather than read as text, so that `\\"`, `\\n` and `\\u2019` are the characters they
    stand for. Values that carry markup are read a second time with their tags removed, since
    an API serving rendered HTML inside a string is common.
    """
    try:
        values = _strings(json.loads(body.decode("utf-8", errors="replace")))
    except (ValueError, RecursionError):
        return Extracted(None, JSON, reason="the page is served as JSON and does not parse as it")
    stripped = [_TAG.sub(" ", html.unescape(value)) for value in values if "<" in value]
    return Extracted(
        "\n".join(values), JSON, by="retrieved.extract/json", second="\n".join(stripped)
    )


def _from_xml(body: bytes) -> Extracted:
    """An XML document with its tags removed, as an HTML page's are.

    A CDATA section is unwrapped first. Its opening marker contains no `>`, so the tag pattern
    would otherwise run from it to the end of the first tag inside and take the text between.
    A feed often carries its entries as escaped HTML, which is text as far as XML is concerned,
    so the text is read a second time unescaped and stripped again.
    """
    text = _TAG.sub(" ", _CDATA.sub(r" \1 ", body.decode("utf-8", errors="replace")))
    second = _TAG.sub(" ", html.unescape(text)) if "&lt;" in text else ""
    return Extracted(
        text, XML, by="retrieved.extract/xml", second=f"{second}\n{_without_tex_type(text)}"
    )


def _pdftotext_version(binary: str) -> str:
    """`pdftotext 24.02.0`, or `pdftotext` alone where it will not say.

    Asked after a successful extraction and never before, so a machine without the binary and a
    PDF that failed both cost one process rather than two.
    """
    try:
        said = subprocess.run(  # noqa: S603 - a resolved path and a fixed flag
            [binary, "-v"], capture_output=True, timeout=PDFTOTEXT_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "pdftotext"
    match = _VERSION.search((said.stderr + said.stdout).decode("utf-8", errors="replace"))
    return f"pdftotext {match.group(1)}" if match else "pdftotext"


def _remove(path: str) -> None:
    """Remove the temporary file. One that is already gone is the outcome wanted."""
    try:
        os.unlink(path)
    except OSError:
        return


def _from_pdf(body: bytes) -> Extracted:
    """A PDF's text from `pdftotext`, where one is installed.

    Without `-layout`. That flag keeps the columns of the printed page, padding lines with
    spaces and setting a second column's text beside the first's; reading order is what a
    sentence is found in. The default also rejoins a word hyphenated across a line end, which
    `answer.occurs` allows for.

    Through a temporary file rather than standard input, because only some builds of the
    program read a PDF from a pipe and the rest would report every document unreadable.
    """
    binary = shutil.which("pdftotext")
    if binary is None:
        return Extracted(
            None, PDF, reason="the page is a PDF and no pdftotext is installed to read it"
        )
    handle, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(body)
        ran = subprocess.run(  # noqa: S603 - a resolved path, fixed flags and a path made here
            [binary, "-q", "-enc", "UTF-8", path, "-"],
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Extracted(
            None, PDF, reason=f"pdftotext did not finish in {PDFTOTEXT_TIMEOUT:.0f} seconds"
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Extracted(None, PDF, reason=f"pdftotext could not be run: {type(error).__name__}")
    finally:
        _remove(path)
    if ran.returncode != 0:
        return Extracted(
            None, PDF, reason=f"pdftotext could not read the PDF (exit {ran.returncode})"
        )
    text = ran.stdout.decode("utf-8", errors="replace")
    if not text.strip():
        # A scan has pages and no text layer. That is a limit of the extraction, and reporting
        # it as a page with nothing on it would be a claim about the document.
        return Extracted(
            None, PDF, reason="pdftotext found no text in the PDF, which may be scanned pages"
        )
    # The bytes as served are searched too: a PDF's title and author sit in its metadata, often
    # uncompressed, and a fetch tool that could not read the pages reports those.
    return Extracted(
        text, PDF, by=_pdftotext_version(binary), second=body.decode("utf-8", errors="replace")
    )


def _second_html(retrieval: Retrieval) -> str:
    """More readings of an HTML page: without its formulas' TeX, without TeX that sets type,
    and as served.

    As served, because the fetch tool reads more of a page than its visible text. An image's
    `alt`, a `meta` description and a document's title in a script are all words the page holds
    and `normalize` drops, and a passage found only there is still in these bytes.
    """
    served = retrieval.body.decode("utf-8", errors="replace")
    plain = normalize(_ANNOTATION.sub(" ", served).encode(), retrieval.content_type)
    return f"{plain}\n{_without_tex_type(plain)}\n{served}"


def extract(retrieval: Retrieval) -> Extracted:
    """The text of one retrieval for the answer check, by what the page was served as.

    A PDF is also recognised by its first bytes, because one served as
    `application/octet-stream` is common and nothing else begins `%PDF-`.
    """
    mime = _mime(retrieval.content_type)
    if "pdf" in mime or retrieval.body.startswith(b"%PDF-"):
        return _from_pdf(retrieval.body)
    if "html" in mime:
        return Extracted(
            normalize(retrieval.body, retrieval.content_type),
            HTML,
            by="retrieved.normalize/html",
            second=_second_html(retrieval),
        )
    if "json" in mime:
        return _from_json(retrieval.body)
    if "xml" in mime:
        return _from_xml(retrieval.body)
    if mime.startswith("text/"):
        return Extracted(
            normalize(retrieval.body, retrieval.content_type), TEXT, by="retrieved.normalize/raw"
        )
    served_as = retrieval.content_type or "no stated content type"
    return Extracted(
        None, reason=f"the page is {served_as}, which is not text this library extracts"
    )


__all__ = ["HTML", "JSON", "PDF", "TEXT", "XML", "Extracted", "extract"]
