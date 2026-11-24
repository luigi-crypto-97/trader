"""Parser for EDGAR full-submission text files (``<accession>.txt``).

The full submission contains the SEC header (with the acceptance timestamp, which is what makes
the data point-in-time) and every document of the filing. EDGAR timestamps are US Eastern time.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

_HEADER_RE = re.compile(r"<SEC-HEADER>(.*?)</SEC-HEADER>", re.S)
_ACCEPT_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")
_DOC_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S)
_TYPE_RE = re.compile(r"<TYPE>([^\n<]+)")
_FILENAME_RE = re.compile(r"<FILENAME>([^\n<]+)")
_TEXT_RE = re.compile(r"<TEXT>(.*?)</TEXT>", re.S)
_XML_RE = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.S)


class SubmissionParseError(ValueError):
    pass


@dataclass(frozen=True)
class Document:
    type: str
    filename: str
    text: str

    @property
    def xml(self) -> str | None:
        m = _XML_RE.search(self.text)
        return m.group(1) if m else None


@dataclass(frozen=True)
class Submission:
    accession: str
    form: str
    accepted_at: datetime
    filed_date: date
    items: tuple[str, ...]
    ciks: tuple[int, ...]
    documents: tuple[Document, ...] = field(repr=False)

    def first(self, doc_type: str) -> Document | None:
        return next((d for d in self.documents if d.type == doc_type), None)

    def by_prefix(self, prefix: str) -> list[Document]:
        return [d for d in self.documents if d.type.startswith(prefix)]


def _header_value(header: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", header, re.M)
    return m.group(1) if m else None


def parse_acceptance(raw: str) -> datetime:
    """``YYYYMMDDHHMMSS`` in US Eastern time -> aware UTC datetime."""
    local = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=EASTERN)
    return local.astimezone(UTC)


def parse_submission(text: str) -> Submission:
    header_m = _HEADER_RE.search(text)
    if not header_m:
        raise SubmissionParseError("missing <SEC-HEADER>")
    header = header_m.group(1)
    accept_m = _ACCEPT_RE.search(text)
    if not accept_m:
        raise SubmissionParseError("missing <ACCEPTANCE-DATETIME>")
    accession = _header_value(header, "ACCESSION NUMBER")
    form = _header_value(header, "CONFORMED SUBMISSION TYPE")
    filed = _header_value(header, "FILED AS OF DATE")
    if not (accession and form and filed):
        raise SubmissionParseError("header lacks accession, form type or filing date")
    items = tuple(m.strip() for m in re.findall(r"^\s*ITEM INFORMATION:\s*(.+?)\s*$", header, re.M))
    ciks = tuple(dict.fromkeys(int(c) for c in re.findall(r"CENTRAL INDEX KEY:\s*(\d+)", header)))
    documents = []
    for body in _DOC_RE.findall(text):
        t = _TYPE_RE.search(body)
        fn = _FILENAME_RE.search(body)
        tx = _TEXT_RE.search(body)
        documents.append(
            Document(
                type=t.group(1).strip() if t else "",
                filename=fn.group(1).strip() if fn else "",
                text=tx.group(1) if tx else "",
            )
        )
    return Submission(
        accession=accession,
        form=form,
        accepted_at=parse_acceptance(accept_m.group(1)),
        filed_date=datetime.strptime(filed, "%Y%m%d").date(),  # noqa: DTZ007 - calendar date only
        items=items,
        ciks=ciks,
        documents=tuple(documents),
    )


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_BLOCK_RE = re.compile(r"</?(p|div|br|tr|li|h[1-6]|table)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\xa0]+")
_NL_RE = re.compile(r"\n\s*\n+")


def html_to_text(raw: str) -> str:
    """Readable plain text from filing HTML (good enough for LLM input and quote checks)."""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()
