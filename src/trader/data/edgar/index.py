"""Parsers for EDGAR filing lists: daily master index and the 'latest filings' Atom feed."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime

ATOM = "{http://www.w3.org/2005/Atom}"
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
_CIK_IN_PATH_RE = re.compile(r"/edgar/data/(\d+)/")


@dataclass(frozen=True)
class IndexEntry:
    cik: int
    company: str
    form: str
    filed: date
    path: str  # e.g. edgar/data/1000045/0001000045-24-000123.txt

    @property
    def accession(self) -> str:
        m = _ACCESSION_RE.search(self.path)
        if not m:
            raise ValueError(f"no accession number in {self.path}")
        return m.group(1)


def parse_master_index(text: str) -> list[IndexEntry]:
    entries: list[IndexEntry] = []
    started = False
    for line in text.splitlines():
        if not started:
            started = line.startswith("-----")
            continue
        parts = line.split("|")
        if len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        cik, company, form, filed, path = (p.strip() for p in parts)
        filed_date = (
            datetime.strptime(filed, "%Y%m%d").date()  # noqa: DTZ007 - calendar date only
            if len(filed) == 8
            else date.fromisoformat(filed)
        )
        entries.append(IndexEntry(cik=int(cik), company=company, form=form, filed=filed_date, path=path))
    return entries


def parse_latest_atom(xml: str, form: str) -> list[IndexEntry]:
    """Entries of the 'getcurrent' Atom feed whose form type equals ``form`` exactly
    (the feed's ``type`` filter is a prefix match: type=4 also returns 424B2)."""
    root = ET.fromstring(xml)
    entries: list[IndexEntry] = []
    for entry in root.findall(f"{ATOM}entry"):
        category = entry.find(f"{ATOM}category")
        term = category.get("term", "") if category is not None else ""
        if term != form:
            continue
        link = entry.find(f"{ATOM}link")
        href = link.get("href", "") if link is not None else ""
        entry_id = (entry.findtext(f"{ATOM}id") or "").strip()
        acc = _ACCESSION_RE.search(entry_id) or _ACCESSION_RE.search(href)
        cik = _CIK_IN_PATH_RE.search(href)
        updated = (entry.findtext(f"{ATOM}updated") or "").strip()
        if not (acc and cik and updated):
            continue
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        company = title.split(" - ", 1)[1].rsplit(" (", 2)[0] if " - " in title else title
        entries.append(
            IndexEntry(
                cik=int(cik.group(1)),
                company=company,
                form=term,
                filed=datetime.fromisoformat(updated).date(),
                path=f"edgar/data/{int(cik.group(1))}/{acc.group(1)}.txt",
            )
        )
    return entries
