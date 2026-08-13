"""sgtestpaper.com adapter.

Observed structure (selectors written against fetched HTML, not guessed):

  /gce/
      links to per-year, per-subject index pages named
      `gce/<year>/y<yy>_jc2_maths_h2_jc_exam_papers.html`
  year index
      links to per-school detail pages named
      `y<yy>_jc2_Maths_h2_prelim_<CODE>.html`
  school detail page
      a single `<a href="...zip">Download this test paper now!</a>` pointing at
      `/gce/test_papers_<year>/jc2_math_h2_<year>/JC2_Maths_H2_<year>_<CODE>.zip`
  zip
      one PDF per paper/doc-type, e.g. `EJC_9758_2025_Prelim_P1_Solutions.pdf`

Pages are served as UTF-16 with a BOM; `Fetcher` sniffs that.
"""

from __future__ import annotations

import re

from ..archive import ZipPeekUnsupported, peek_zip_members
from ..util import detect_doc_type, detect_paper_no, detect_school
from .base import PaperLink, SourceAdapter, register

_YEAR_INDEX_RE = re.compile(
    r"y(\d{2})_jc2_maths_h2_jc_exam_papers?\.html", re.IGNORECASE
)
_SCHOOL_PAGE_RE = re.compile(
    r"y(\d{2})_jc2_maths_h2_prelim_([A-Za-z]+)\.html", re.IGNORECASE
)


@register
class SgTestPaper(SourceAdapter):
    name = "sgtestpaper"
    base_url = "https://www.sgtestpaper.com"
    index_url = "https://www.sgtestpaper.com/gce/"

    def discover(self) -> list[PaperLink]:
        target = int(self.cfg.get("discover_target_exams", 8))
        max_years = int(self.cfg.get("max_years", 2))

        links: list[PaperLink] = []
        exams: set[str] = set()

        # Bounded by max_years so a source that stops yielding links (bad
        # selectors, a layout change) cannot walk the whole archive.
        for year, year_url in self._year_indexes()[:max_years]:
            for school_url in self._school_pages(year_url):
                if len(exams) >= target:
                    return self.sort_newest_first(links)
                for link in self._links_for_school(year, school_url):
                    links.append(link)
                    exams.add(link.exam_key)

        return self.sort_newest_first(links)

    # -- level 1: year/subject index pages ------------------------------
    def _year_indexes(self) -> list[tuple[int, str]]:
        try:
            soup = self._soup(self.index_url)
        except Exception as exc:
            self._note_error(f"index {self.index_url}: {type(exc).__name__}: {exc}")
            return []

        found: dict[int, str] = {}
        for a in soup.find_all("a", href=True):
            m = _YEAR_INDEX_RE.search(a["href"])
            if m:
                found.setdefault(2000 + int(m.group(1)), self._abs(a["href"]))
        return sorted(found.items(), reverse=True)

    # -- level 2: per-school detail pages -------------------------------
    def _school_pages(self, year_url: str) -> list[str]:
        try:
            soup = self._soup(year_url)
        except Exception as exc:
            self._note_error(f"year page {year_url}: {type(exc).__name__}: {exc}")
            return []
        urls: list[str] = []
        for a in soup.find_all("a", href=True):
            if _SCHOOL_PAGE_RE.search(a["href"]):
                url = self._abs(a["href"])
                if url not in urls:
                    urls.append(url)
        return urls

    # -- level 3: the zip and its members -------------------------------
    def _links_for_school(self, year: int, school_url: str) -> list[PaperLink]:
        try:
            soup = self._soup(school_url)
        except Exception as exc:
            self._note_error(f"school page {school_url}: {type(exc).__name__}: {exc}")
            return []

        zip_url = None
        for a in soup.find_all("a", href=True):
            if a["href"].lower().endswith(".zip"):
                zip_url = self._abs(a["href"])
                break
        if not zip_url:
            self._note_error(f"no .zip link on {school_url}")
            return []

        code, school = detect_school(school_url, zip_url, soup.title.text if soup.title else "")
        if code == "UNK":
            self._note_error(f"could not identify school for {school_url}")

        try:
            members = peek_zip_members(self.fetcher, zip_url)
        except Exception as exc:
            self._note_error(f"zip peek {zip_url}: {type(exc).__name__}: {exc}")
            return []

        out: list[PaperLink] = []
        for member in members:
            if not member.is_pdf:
                continue
            out.append(
                PaperLink(
                    source=self.name,
                    file_url=zip_url,
                    page_url=school_url,
                    title=member.name,
                    school=school,
                    school_code=code,
                    year=year,
                    exam_type="prelim",
                    paper_no=detect_paper_no(member.name),
                    doc_type=detect_doc_type(member.name),
                    extra={
                        "container": "zip",
                        "member": member.name,
                        "member_bytes": member.uncompressed_size,
                    },
                )
            )
        return out

    def _abs(self, href: str) -> str:
        from urllib.parse import urljoin

        return urljoin(self.index_url, href)
